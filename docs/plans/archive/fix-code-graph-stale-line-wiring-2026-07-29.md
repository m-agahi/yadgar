# Fix code_graph `stale @ <sha>` wiring — the marker is unreachable in production — 2026-07-29

**Task:** task:0067.
**Status:** PLAN — ready to build. One bounded car for the live train `feat/v5.169-install-runtime-fixes`.
**Scope:** core-only. No backend change. Investigation done 2026-07-29 against branch
`feat/v5.169-install-runtime-fixes` (HEAD `93f757f5`); every claim below re-verified against the
current tree, not against the task description.

---

## Context — what is actually broken

`digest.py::_stale_line` renders the freshness marker only when BOTH keys are present:

```python
# yadgar/core/code_graph/digest.py:399-413
def _stale_line(identity: dict[str, Any]) -> list[str]:
    if identity.get("stale") and identity.get("head_sha"):
        short_sha = str(identity.get("head_sha"))[:12]
        return [f"stale @ {short_sha}"]
    return []
```

There is **exactly ONE production producer of an identity dict**, and it sets neither key:

```python
# yadgar/core/cli/code_graph.py:101-104
identity = {
    "canonical_root": idx.get("canonical_root"),
    "subdir": idx.get("subdir", ""),
}
```

`digest.build_block_payload` / `render_digest` (`digest.py:493`, `digest.py:429`) are called from
exactly one place in the product (`cli/code_graph.py:105`) — verified by repo-wide grep. So:

- **The task's hypothesized "asymmetry between producers" does not exist.** There is no second
  producer that populates `stale`/`head_sha` while this one forgets to. The MCP/block path is not a
  producer at all — the block write is Claude calling the generic `block_update` with the payload
  this CLI emitted (`code_graph_refresh_prompt.md` step 3). The auto-injected `code_graph` memory
  block is *content* produced by that same single path.
- The fix shape is therefore **"teach the single producer to populate the optional keys truthfully"**,
  not "make a contract uniform across producers."

**Consequence today:** `stale @ <sha>` can never render in production. The secret-gate false-positive
class that PR #9's `_defang_secret_shaped_runs` defends against has its one reliable live trigger (a
40-hex git SHA) in that line — so the defang fix is correct but its live trigger is latent. This is a
**wiring gap, not a security gap**: the defang fix already handles the case whenever the wiring lands.

### Why it stayed dead: no end-to-end assertion

`yadgar/tests/core/test_code_graph_digest.py:210-225` (`TestStale`) and `:466-474` both hand-build an
identity dict with `stale`/`head_sha` and call `render_digest` directly. They pin the **renderer**,
which works. Nothing anywhere drives `_cmd_refresh` (or the live CLI) and asserts the marker appears —
so the producer→renderer seam has zero coverage and shipped dead. Closing exactly that gap is a
non-negotiable acceptance criterion below.

### What the design intended

ADR-0162 (`status: open`) and the archived plan agree, and describe the **skip** path, not the
success path:

> Guard: no remote / offline / fetch fails → **skip refresh**, keep last digest marked
> `stale @ <sha>`. Never silently index WIP.
> — `docs/plans/archive/code-graph-codebase-memory-mcp-2026-07-22.md:51-52`

> Offline/dirty → skip, keep last digest stale@sha. — ADR-0162, usage-model pivot

That is the semantics the renderer was built for, and the CLI never implemented it. Today
`_cmd_refresh`'s skip branch (`cli/code_graph.py:85-94`) emits
`{"block_name":"code_graph","skipped":true,"reason":…}` and returns; the hook template step 2 then
does nothing. The previously-written block silently keeps serving an aged digest **with no marker at
all** — the exact failure the stale line exists to prevent.

### Related dormancy (not fixed here, recorded so a reviewer doesn't rediscover it)

`runner.list_projects` (`runner.py:220`) and `runner.detect_changes` (`runner.py:226`) — the latter
docstringed "Car C staleness authority" — have **zero callers** anywhere in the tree. They stay
dormant after this car (see Open decision (d)). `scripts/check_dead_capability.py` is
EDGE_CONTRACT-scoped (graph edge types only) and does not flag them.

---

## Decision — stale semantics

**Chosen: Option A — `stale` means "this digest was NOT produced by a successful index of the current
`origin/<default>` in this run."**

| Path | `identity` | Emitted payload |
|---|---|---|
| success (indexed) | `{canonical_root, subdir, head_sha: <rev-parse origin/default>, stale: False}` | `skipped: false`, no marker (correct — fresh) |
| skip `fetch_failed`, **cached index usable** | `{…, head_sha: <last-known sha>, stale: True}` | `skipped: false`, digest re-rendered from the cached index, ends with `stale @ <sha>` |
| skip `opted_out` | — | `skipped: true` (unchanged, bit-for-bit) |
| skip `no_remote_or_default_branch` | — | `skipped: true` (unchanged, bit-for-bit) |
| any guard below unmet | — | `skipped: true` (unchanged, bit-for-bit) |

**`fetch_failed` is the ONLY re-render-eligible reason** — deliberately, not by omission.
`no_remote_or_default_branch` is reached precisely because `resolve_default_branch` returned `None`
(`default_branch.py:133-140`), so there is no `<default>` to interpolate into
`git rev-parse origin/<default>` and no sha is resolvable by construction. Listing it as eligible
would create a table row that can never fire — which is how the original bug got written.

**Guards — re-render on the skip path ONLY when both hold; otherwise behave exactly as today:**

1. `get_architecture` returns non-empty architecture data (a cached index exists for this project), **and**
2. a sha resolves.

Rationale for the guards: never emit a payload we cannot honestly stamp, and never regress a path
that works today into a new failure mode.

### head_sha source: `git rev-parse origin/<default>`, captured in `default_branch.refresh_index`

- Cheap, local, **needs no binary** — so the CI-visible tier-1 test below can exercise the whole seam.
- On the success path the temp worktree materialised `origin/<default>`, so this value **is** the
  indexed snapshot's sha by construction.
- On the `fetch_failed` path it is the **stale local remote-tracking ref** — i.e. precisely "the
  commit the cached index describes." That is the honest value; do not later "correct" it to a
  freshly-fetched remote head (we are offline; there is no such thing to read).
- Ref missing / `rev-parse` fails → **no sha → hard skip**, no marker.

**Deviation to declare:** ADR-0162 says "staleness sig = `head_sha` + node/edge counts from
`list_projects`; incremental via `detect_changes`." This car uses git instead, for the no-binary /
CI-testability reason above. Record the deviation in the ADR addendum (car step 6).

**`_stale_line` stays unchanged.** Its AND-guard is correct and the `_defang_secret_shaped_runs`
short-SHA behaviour depends on the current shape. The producer guarantees a sha whenever it sets
`stale` — no bare `stale @ unknown`.

---

## The car — file seam

Single car, `fix/code-graph-stale-line-wiring`, rebased onto `feat/v5.169-install-runtime-fixes`.

**Code (clean seam — no overlap with any other car):**
- `yadgar/core/code_graph/default_branch.py` — capture `head_sha` into both the success and the
  `fetch_failed` / `no_remote_or_default_branch` return dicts.
- `yadgar/core/cli/code_graph.py` — `_cmd_refresh`: populate `stale`/`head_sha` in `identity`;
  add the guarded stale re-render branch to the skip path.

**Tests:**
- `yadgar/tests/core/test_code_graph_cli.py` — tier-1 (CI-visible) seam tests.
- `yadgar/tests/core/test_code_graph_default_branch.py` — `head_sha` capture, both paths.
- `yadgar/tests/core/test_code_graph_e2e.py` — tier-2 binary-guarded live assertion.

**Prose / docs (⚠ shared-file overlap — see Integration below):**
- `yadgar/core/hooks/templates/code_graph_refresh_prompt.md` — step 2 currently enumerates
  "opted out, no remote, offline, binary absent, or no change" as skip reasons. After this car
  "no remote / offline" can produce a written block. Prose-only correction; the mechanics of step 3
  (`skipped=false` → write) already handle the new payload, so **no logic change to the template**.
- `docs/contracts/BEHAVIOR_CONTRACT.md` — amend BC-CODEGRAPH-4's prose, add BC-CODEGRAPH-7.
- `docs/contracts/CAPABILITY_REGISTRY.md` — CAP-CODEGRAPH-001 `bc:` list + addendum.
- ADR-0162 addendum (via `adr_add`/wiki, not a file edit).
- `docs/CHANGELOG.md` + version bump.

**NOT touched by this car:** `yadgar/core/cli/setup.py`, `yadgar/core/install/**`,
`yadgar/core/code_graph/digest.py`, `yadgar/core/code_graph/config.py`,
`yadgar/core/code_graph/runner.py`.

### Phases

1. **RED (tier-1).** Add the failing seam tests to `test_code_graph_cli.py` (patterns already exist
   at `test_code_graph_cli.py:84-181` — `SimpleNamespace` args + `patch` on
   `default_branch.refresh_index` / `runner.get_architecture` / `runner.fetch_endpoints`). Verify they
   fail for the right reason (no `stale @` in content) before writing code.
2. **`head_sha` capture** in `default_branch.refresh_index` + its unit tests. Note:
   `test_code_graph_default_branch.py:169` patches `_git` wholesale with a `CalledProcessError`
   side-effect, so the new `rev-parse` must be individually failure-tolerant (its failure ⇒ no sha ⇒
   downstream hard skip), and the existing test must stay green unmodified.
3. **`_cmd_refresh` identity + guarded skip re-render.** `opted_out` stays a hard skip — the existing
   `test_refresh_emits_skip_signal_when_index_skipped` (`test_code_graph_cli.py:162`, which uses
   `reason="opted_out"`) must pass **unmodified**. If it needs editing, the guard is wrong.
4. **Tier-2 e2e** in `test_code_graph_e2e.py` + a `skip_inventory` entry (ADR-0087 convention,
   mirroring `code-graph-e2e-smoke-01`).
5. **Hook-template prose fix.**
6. **Contracts + ADR addendum + CHANGELOG + version bump** (`scripts/bump_version.py --bump patch`
   → `sync_version.py` cascade; the repo gates on `check_version_bump.py` / `check_versions.py`).
7. **Gates:** ruff, import-linter, `check_contract_coverage.py`, `check_capability_coverage.py`,
   `check_skip_inventory.py`, `check_observe_coverage.py`, full core test run. Loop until clean.

---

## Acceptance criteria

**AC-1 [unit] — `head_sha` is captured on the success path.**
`refresh_index` on a healthy repo returns a `head_sha` equal to `git rev-parse origin/<default>`.

**AC-2 [unit] — `head_sha` is captured on the fetch-fail path.**
`refresh_index` with a failing `git fetch` but a present `refs/remotes/origin/<default>` returns
`{skipped: True, reason: "fetch_failed", head_sha: <local remote-tracking sha>}`. With the ref absent,
`head_sha` is absent/empty. (`no_remote_or_default_branch` carries no `head_sha` — see the
decision table.)

**AC-3 [unit] — a fresh digest carries no marker.**
`_cmd_refresh` on the success path emits `skipped: false` and `"stale @" not in content`, with
`identity["stale"] is False` and a non-empty `head_sha`.

**AC-4 [e2e, CI-visible] — THE criterion this plan exists for. The marker renders end-to-end on a
stale digest.**
Drive the production seam `cmd_code_graph(SimpleNamespace(cg_command="refresh", json=True, …))` with
the runner boundary patched: `refresh_index` → `{"skipped": True, "reason": "fetch_failed",
"canonical_root": …, "subdir": "", "head_sha": "<40-hex>"}`, `get_architecture` → fixture
architecture, `fetch_endpoints` → `[]`. Assert the emitted payload has `skipped is False` **and**
`"stale @ " in payload["content"]` **and** the rendered short sha is the first 12 chars of the input
sha. This runs in CI on every commit — it exercises the real `_cmd_refresh` identity construction and
the real `render_digest`, which is exactly where the bug lives.

> Fixture constraint (load-bearing): the fixture architecture MUST contain no `[A-Za-z0-9/+]` run of
> ≥40 chars. `render_digest` runs `_defang_secret_shaped_runs` over the joined text
> (`digest.py:474`), which would insert a space mid-run and make the exact-12-char assertion fail for
> a reason unrelated to what AC-4 tests. The 40-hex `head_sha` itself is safe — `_stale_line` cuts it
> to 12 chars *before* the defang pass ever sees it (that is the #30 fix working as designed), and
> asserting the 12-char prefix is precisely what proves it.

> Tiering note (deliberate): `test_code_graph_e2e.py` is
> `skipif(shutil.which("codebase-memory-mcp") is None)` (`:37-40`) and **never runs in CI** (259 MB
> host-side dep), and `scripts/check_e2e_assertions.py` only scans `yadgar/tests/e2e/`, so nothing
> lints it either. An assertion placed only there would repeat the original sin one level up: a test
> written to prevent "shipped dead" that is itself dead in CI. AC-4 is therefore the CI-visible tier
> and is mandatory; AC-5 is belt-and-braces.

**AC-5 [e2e, binary-guarded] — real-binary confirmation.**
In `test_code_graph_e2e.py`: build the hermetic repo, index it once successfully, then break the
remote (e.g. point `origin` at a non-existent path) so `git fetch` fails, re-run
`refresh --json`, and assert `skipped is False` and `"stale @ " in content`. Guarded by the existing
`shutil.which` module-level skip + a `skip_inventory` entry.

> Assert the reason is `fetch_failed`, not merely "some skip." `resolve_default_branch` runs BEFORE
> the fetch (`default_branch.py:133`) and reads `refs/remotes/origin/HEAD`, which survives a broken
> remote *path* — so breaking the path should land on `fetch_failed`. If the chosen breakage instead
> trips `resolve_default_branch` first, the test takes the `no_remote_or_default_branch` branch and
> passes for the wrong reason (hard skip, no marker). Pin the reason so that cannot hide.

**AC-6 [unit] — no regression on the hard-skip paths.**
`opted_out` still emits `{"block_name":"code_graph","skipped":true,"reason":"opted_out"}` and calls
neither `get_architecture` nor `fetch_endpoints`
(`test_code_graph_cli.py::test_refresh_emits_skip_signal_when_index_skipped` passes **unmodified**).
Plus new cases: `fetch_failed` with empty architecture → hard skip; `fetch_failed` with no resolvable
sha → hard skip.

**AC-7 [unit] — BC-CODEGRAPH-4 still holds at its own layer.**
`test_code_graph_default_branch.py::TestSkipGuards::test_fetch_failure_skips_not_fallback` passes
unmodified: `refresh_index` still returns `skipped: True` and still never indexes the WIP tree. Only
the *CLI* re-emits a marked digest — the indexer contract is untouched.

**AC-8 [manual] — hook-template prose matches behaviour.**
`code_graph_refresh_prompt.md` step 2 no longer claims "no remote / offline" always means "nothing to
do."

**AC-9 [manual] — contracts updated and coverage gates green.**
BC-CODEGRAPH-4 prose amended; BC-CODEGRAPH-7 added with a test ref;
CAP-CODEGRAPH-001 `bc:` list extended; `check_contract_coverage.py` +
`check_capability_coverage.py` pass.

Proposed row:

> BC-CODEGRAPH-7 — when `code-graph refresh` cannot re-index (`fetch_failed` /
> `no_remote_or_default_branch`) but a cached index and a resolvable `head_sha` exist, it SHALL
> re-emit the cached digest with a trailing `stale @ <12-char sha>` marker and `skipped: false`;
> when either is absent, or the reason is `opted_out`, it SHALL hard-skip with a reason and emit no
> digest.

And the BC-CODEGRAPH-4 clarification: skip means *never index the WIP tree* — the CLI MAY re-emit the
**cached** digest marked stale.

---

## Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | Behaviour change: a skip that used to write nothing can now trigger a block write. | Bounded to the single reason `fetch_failed`, doubly guarded (cached index + resolvable sha). `opted_out`, `no_remote_or_default_branch` and binary-missing unchanged. AC-6 pins it. |
| R2 | The re-render calls `get_architecture` on the skip path — a subprocess where today there is none. | Only on `fetch_failed`, which implies git failed, not the binary. Empty/error result ⇒ hard skip (AC-6). One extra ~0.5 s call on an already-degraded path. |
| R3 | `project` key is absent from `refresh_index`'s skip returns, so the re-render falls back to `Path(repo).resolve().name`. | That fallback is already the live path elsewhere (`cli/code_graph.py:97`, `test_code_graph_e2e.py:166`); ADR-0162 records the `project` passthrough as UNVERIFIED against the real binary regardless. Wrong project ⇒ empty architecture ⇒ hard skip, which is today's behaviour. Do **not** reach for `list_projects` here. |
| R4 | Doc-file collision with the already-merged install car (`a94ec3cd` added BC-CODEGRAPH-6 and the CAP-CODEGRAPH-001 addendum). | Append-only after the existing BC-CODEGRAPH block; do not edit BC-CODEGRAPH-6's line. Rebase on the train before pushing. |
| R5 | While offline, every stop-hook cadence (200 msgs) rewrites the same stale-marked block. | Idempotent — identical content, same block. See open decision (c). |
| R6 | The digest still carries no sha at all when fresh, so a reader cannot tell which commit it describes. | Out of scope by choice — see open decision (b). |
| R7 | `test_code_graph_default_branch.py:169` patches `_git` wholesale; a new `rev-parse` inherits the `CalledProcessError`. | Make `rev-parse` individually failure-tolerant; AC-7 requires that test to pass unmodified. |

---

## Open decisions for the user (one recommendation each)

**(a) Stale semantics.**
- **Recommend: Option A** (above) — stale = "could not re-index this run, serving a cached digest."
  Directly implements the archived plan's line 51-52 and ADR-0162's "offline/dirty → skip, keep last
  digest stale@sha."
- Rejected alternative: stale = "your working-tree HEAD differs from the indexed `origin/<default>`."
  It would fire on essentially every feature branch and contradicts ADR-0162's explicit intent that
  the digest is master-canonical and "stable while the user branch-switches."
- Rejected alternative: stamp `stale` on the success path from `detect_changes`. By construction a
  just-completed index is not stale — this would keep the marker dead, i.e. a non-fix.

**(b) Surface `head_sha` on FRESH digests too** (e.g. in the header line), so a reader always knows
which commit the digest describes.
- **Recommend: defer** to a named follow-up. It touches the header and every golden digest test —
  real value, but a different car. Flagged so the fresh-path asymmetry is explained, not accidental.

**(c) Re-writing the same stale-marked block every cadence while offline.**
- **Recommend: accept.** The write is idempotent (identical content to the same block), and a reader
  that only sees the injected block has no other freshness signal.

**(d) Wire `detect_changes` / `list_projects` (ADR-0162's nominal staleness authority).**
- **Recommend: no, not in this car.** Both stay dormant. Using them would require the 259 MB binary
  and would make AC-4 un-runnable in CI. Revisit if a future car needs incremental reindexing.

---

## Integration notes for the train

- **Branch:** `fix/code-graph-stale-line-wiring`, rebased onto `feat/v5.169-install-runtime-fixes`.
- **Code seam is disjoint** from every other car in the train. `yadgar/core/cli/setup.py` and
  `yadgar/core/install/**` are **not touched** — the merged install-automation car (`904f1982` /
  `a94ec3cd`) and this one do not collide in code.
- **Only collision is docs:** `BEHAVIOR_CONTRACT.md` and `CAPABILITY_REGISTRY.md`, both already
  edited by that merged car. Sequence this car **after** it (it is already in the train) and keep the
  edits append-only.
- Removed-surface reminder: the `--code-graph` flag and the `CODE_GRAPH_ENABLED` runtime env var are
  **gone**; enable/opt-out is the `code_graph.enabled` runtime-config row (ADR-0163). Nothing in this
  plan depends on either.
- **Handoff:** `docs/plans/ROADMAP.md` registration is required by repo convention but is
  deliberately not done here — the integrator/user adds the row (Infra/ops or Active table).
