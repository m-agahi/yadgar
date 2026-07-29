# Fix the gate-blindness class: three guards that passed while covering nothing

**Date:** 2026-07-29
**Tasks:** I32 registry rot (from task:0082) · mocked-route fiction (from task:0045) · e2e-lint scan root (from task:0067)
**Status:** PLANNED — investigation complete, awaiting decisions D1–D5 before implementation.
**Target train:** `feat/v5.169-install-runtime-fixes` (or a successor train — see D5).
**Scope guard:** this plan designs GUARDS ONLY. The vacuum `/api/check_invariants` bug itself is owned by
`car/vacuum-reclaim` / `docs/plans/fix-vacuum-reclaim-and-core-stability-2026-07-29.md`. Nothing here touches
`yadgar/core/vacuum/**`.

---

## 0. Verdict up front

| # | Defect | Verdict | Why |
|---|---|---|---|
| **D1** | I32 capability-registry gate blind to pure-code changes | **BUILD GUARD (with a stated ceiling)** | The gate is **assertion-blind, not trigger-blind** — see §1.1. Prose-token liveness is the whole fix. Baseline cost measured: 19 allowlist entries. |
| **D2** | Mocked-endpoint tests validated a route that never existed | **BUILD GUARD — strongest signal-to-noise of the three** | Measured: 75 path literals collected, **4** unresolved, 1 of which is the real bug. The "cries wolf" fear does not materialise at this repo's scale. Blocked on sequencing (D5). |
| **D3** | `check_e2e_assertions.py` scans only `yadgar/tests/e2e/` | **BUILD GUARD — mechanical, lowest risk** | 6 `*_e2e*` modules live outside the scan root. Widening lands **green** (0 violations across all 6), so the car is a pure widening with no fix-up work. `check_test_weakening.py` must be widened in lockstep — it has a *second*, independent scan-root pin in its own regex. |
| **D4** | `check_test_weakening` runs in CI but is a **structural no-op** there | **BUILD GUARD — smallest car, purest instance of the class** | It reads `git diff --cached`; a CI checkout has nothing staged, so it executes, prints "OK", and **cannot fail**. Measured, §5. |

**Meta-guard over `.pre-commit-config.yaml` ("whole-repo checker ⇒ `always_run: true`"): NOT WORTH BUILDING.**
Evidence in §1.3 — the genuinely-blind set is 3 hooks, and 2 of those become blind only as a *consequence* of
this plan's own widening. Fix the instances; skip the meta-guard.

---

## 1. Are these one plan or three?

### 1.1 The task's framing does not survive the evidence

The brief proposed a shared mechanism: "guards that key on the wrong trigger surface." Two of the three
defects do not have that shape.

**D1 is not trigger-blind.** `.forgejo/workflows/ci-pr.yaml:411-412` runs
`python scripts/check_capability_coverage.py` unconditionally inside the `invariant-checks` job — no `files:`
predicate, no path filter. On the PR containing `7cd74ea0` (the commit that made CAP-CODEGRAPH-001's prose
false) **the I32 checker ran and returned clean.** Widening the pre-commit trigger to `always_run: true` would
not have changed that, because the checker would still have executed and still returned clean.

The hole is entirely in `check()` (`scripts/check_capability_coverage.py:308-339`): it covers the four
identifier surfaces (settings / tools / migrations / BC) and **never reads the `explanation:` or `wiring:`
prose**, which is where the false claim lived. The script's own docstring is honest about this
(`:20-25`: "DOES NOT GUARANTEE ... status accuracy is a human/review responsibility") — the gap is documented,
just not gated.

**D3 is genuinely trigger-blind** — the opposite failure from D1. `scripts/check_e2e_assertions.py:28` pins
`_E2E_DIR` to one directory; six `*_e2e*` modules live outside it and are never linted. The checker's read
set and its `files:` filter agree with each other — and both are narrower than the artifact class the lint
claims to cover.

**D2 has neither a trigger nor an assertion.** There was no guard at all. It is a greenfield build, not a
repair.

**D4 has a correct trigger, a correct scope, and an assertion that cannot fire in the environment where it
matters most** (§5).

Four different failure modes: *assertion-scope < claim-scope* (D1), *no guard* (D2), *scan-scope <
artifact-scope* (D3), *assertion inert in CI* (D4).

### 1.2 A correction worth recording, because it nearly became a fifth car

An earlier pass of this investigation grepped only `.forgejo/workflows/ci-pr.yaml` + `.github/workflows/` for
each lint's name and concluded that **8 of 12 repo-local lints have no CI presence**, including I13, I26, I28
and I30. **That conclusion was wrong.** `.forgejo/workflows/validate.yaml:45-46` (and its mirror
`.github/workflows/validate.yml:67-73`) runs `pre-commit run --all-files` on every `pull_request` to `master`,
unfiltered. Every local hook therefore does execute in CI — just from `validate`, not from
`ci-pr`'s `invariant-checks` job.

Recorded because the near-miss is itself an instance of the class: *a check that looks for the wrong trigger
surface reaches a confident false conclusion.* The residue of that investigation is D4 — the one hook that
runs under `--all-files` and still cannot fail.

Minor doc nit that falls out: `docs/ARCHITECTURE_INVARIANTS.md` says the lints are "wired in
`.pre-commit-config.yaml` (local hooks) + `.forgejo/workflows/ci-pr.yaml` (`invariant-checks`)". Six run in
`invariant-checks`; the rest reach CI via `validate.yaml`. Substantively correct, imprecise about the path.
Fix in passing, not worth a car.

### 1.3 So is there a shared pattern?

No single mechanism unites the four. What unites them is a *review question* worth adding to the plan
template, not a lint: **"name the input change that makes this gate fail."** Each of these four had no
answer — D1's failing input is prose the checker never reads; D2's is a URL nothing compares; D3's is a file
outside the scan root; D4's is a staged diff that CI never has.

### 1.4 Why the meta-guard is not worth building

The candidate meta-guard: a lint over `.pre-commit-config.yaml` asserting that any hook with
`pass_filenames: false` (i.e. a whole-repo checker) must also carry `always_run: true`. Enumerating the 14
such hooks against each script's actual on-disk read set:

| Hook | `files:` trigger | Script read set | Blind? |
|---|---|---|---|
| `sync-version`, `sync-uv-lock` | `pyproject.toml` | pyproject → server.json / uv.lock | No — sync source is the trigger |
| `check-versions`, `check-backend-bump` | *(none)* | whole repo / branch diff | No — already `always_run` (ADR-0080) |
| `check-metric-writers` | `^yadgar/.*\.py$` | all of `yadgar/` | No — trigger ⊇ read set |
| `check-trace-spans` | `server/http\.py` | `http.py` only | No — correctly narrow |
| `check-observe-coverage` | `^yadgar/.*\.py$` + allowlist | all of `yadgar/` + allowlist | No |
| `check-dynamic-span-names` | `^yadgar/.*\.py$` | all of `yadgar/` | No |
| `check-secret-gate` | `tools/.*\.py$` | `tools_dir.glob("*.py")` | No (but see §6 note — `glob` not `rglob`) |
| `check-allowlist-audit` | `security/(allowlist|secrets)\.py` | exactly those two files | No |
| `check-dead-capability` | 4 named files | exactly those files | No |
| `check-complexity-allowlist` | `yadgar/*.py`, `scripts/*.py`, 2 json | those | No |
| `check-skip-inventory` | `yadgar/tests/**` | all tests + inventory | No |
| `check-subsystem-readmes` | subsystem dirs | subsystem dirs | No |
| `check-capability-coverage` | 5 named files | those 5 — **today** | **Becomes blind only after D1's widening** |
| `check-e2e-assertions` | `tests/e2e/**` | `tests/e2e/**` — **today** | **Becomes blind only after D3's widening** |
| `check-test-weakening` | `tests/e2e/**` + contract | staged diff, regex-matched on `tests/e2e/` | **Yes, doubly — filter AND internal regex** |

Genuinely blind today: **one** (`check-test-weakening`). Two more become blind as a direct consequence of
widening their own checkers in this plan — which the cars widen in lockstep anyway. A meta-guard whose
day-one finding count is 1, over a config file that changes a handful of times per year, is a lint nobody
will read. **Not worth building.** Recorded here so the question does not get re-asked.

### 1.5 Recommendation

**One plan, four independent cars, no shared mechanism.** They are grouped because they were all found the
same day and all touch `scripts/*` + `.pre-commit-config.yaml` (so serialising them avoids merge collisions on
that file), **not** because a single fix addresses them. Do not manufacture a shared root cause — the evidence
does not support one.

Car 2 (§3) carries the highest measured value: it is the only one of the four with a proven live production
bug in its baseline. Car 4 (§5) is the cheapest. Car 3 (§4) is mechanical and lands green.

---

## 2. Car 1 — I32 prose-token liveness (D1)

### 2.1 What actually rotted

CAP-CODEGRAPH-001's `explanation:` claimed `CODE_GRAPH_ENABLED` "survives only in `cli/setup.py` as a
host-binary INSTALL trigger". Commit `7cd74ea0` (`fix(setup): install code_graph by default, unattended`) —
a **pure code change, zero contract files staged** — removed that last read. The registry stayed wrong until
`a94ec3cd` corrected it by hand, ~2h later, only because a human noticed.

### 2.2 Why a naive liveness check fails, and the refinement that works

Measured at `7cd74ea0` (post-code-change, pre-doc-fix), `git grep -w CODE_GRAPH_ENABLED -- yadgar/` still
returned **4 non-test files** — so a text-grep liveness check would have stayed green. All four hits are
**docstrings or comments**:

- `yadgar/core/cli/setup.py:56` — module docstring
- `yadgar/core/code_graph/__init__.py:21-22` — module docstring
- `yadgar/core/code_graph/config.py:19-20`, `:157` — module docstring + inline comment
- `yadgar/core/hooks/stop-memory-checkpoint.py:284` — `#` comment

The refinement: liveness must be measured over **executable code only**. AST gives this for free — comments
never enter the AST, and module/class/function docstrings are identifiable as the first `Expr(Constant(str))`
of a body. Under that definition `CODE_GRAPH_ENABLED` was dead at `7cd74ea0` and the guard fires.

Note as a secondary finding: the `config.py:19-20` docstring carried the *same false claim* as the registry.
Docstring prose rots identically. This car does not gate docstrings (see §8 R3).

### 2.3 Design — `scripts/check_registry_prose_liveness.py`

New script; do **not** bolt this onto `check_capability_coverage.py` (that script's contract is
catalogue-completeness over four enumerable surfaces; mixing a heuristic prose check into it muddies a clean
invariant and its documented non-guarantees at `:20-25`).

1. **Collect claims.** Every backtick-quoted token in `docs/contracts/CAPABILITY_REGISTRY.md` matching
   `` `([A-Z][A-Z0-9_]{4,})` `` **and containing at least one `_`** (the `_` requirement drops the English
   words `SHADOW`, `DORMANT`, `WRITE` that are status-enum values, not identifiers). Measured: 382 candidates
   before the `_` filter.
2. **Collect liveness.** AST-walk every non-test `.py` under `yadgar/` (plus the extensionless hook scripts
   under `yadgar/core/hooks/`), collecting `Name.id`, `Attribute.attr`, and identifier-shaped substrings of
   `Constant(str)` values — **excluding** every node identified as a docstring. Union with a plain token scan
   of `flake.nix`, `pyproject.toml`, `Dockerfile*`, `docker-compose.yml` (env vars legitimately live there).
3. **Report.** A claimed token with no liveness hit and no allowlist entry is a violation:
   `DEAD-CLAIM: CAPABILITY_REGISTRY cites \`FOO\` but no executable code references it`.
4. **Allowlist** `.registry-prose-allowlist.json`, mirroring `.complexity-allowlist.json` /
   `.observe-allowlist.json` conventions: `{token: {rationale}}`, rationale **≥40 chars**, and a
   **stale entry is a hard error** (an allowlisted token that comes back to life must be de-allowlisted).
   That stale rule is what keeps the allowlist from becoming a write-only dumping ground.

**Measured baseline (worktree, today):** 19 unresolved →
`BAZ_QUX`, `CODE_GRAPH_ENABLED`, `COMPRESSION_GIST_AGE_HOURS`, `COMPRESSION_TAG_AGE_HOURS`,
`CONFIDENCE_GATING_ENABLED`, `CONSOLIDATION_COOLDOWN_SECONDS`, `DAEMON_CHECK_INTERVAL`, `DORMANT`,
`DUAL_VECTORS_ENABLED`, `EMBEDDING_CACHE_SIZE`, `FOO_BAR`, `FRACTAL_LEVELS`, `IDLE_THRESHOLD_SECONDS`,
`QUERY_PREFIX`, `REPO_WIKI_REFRESH_STOP_INTERVAL`, `SHADOW`, `VIZ_PRECOMPUTED_LAYOUT_ENABLED`, `WRITE`,
`WRRF_K`.
After the `_`-containing filter: **16**. Two of those (`FOO_BAR`, `BAZ_QUX`) are the registry's own worked
examples. **~14 real archaeology entries** — same order of magnitude as the existing I30 allowlist. Cheap.

**Runtime:** the prototype full-AST scan measured **1.2s** (and that included a `git show` subprocess per
file; a direct-read version is faster). `check_capability_coverage.py` itself is 0.17s. Both fine for
pre-commit.

### 2.4 Wiring

- `.pre-commit-config.yaml`: new hook, `pass_filenames: false`, **`always_run: true`**. The read set is all
  of `yadgar/` plus the registry, so any narrower `files:` filter is blind by construction (this is the one
  place ADR-0080's mechanism genuinely applies — not as the fix, but as correct scoping for a *new* hook).
- `.forgejo/workflows/ci-pr.yaml`: new step in `invariant-checks`, alongside the existing I32 step.
- Also add `always_run: true` to the existing `check-capability-coverage` hook. This is a **local/CI parity
  nit, not the fix** — CI already runs it unconditionally, so this only removes the class of surprise
  ADR-0080 names ("quiet locally, loud in CI"). Do not let this line item be mistaken for the guard.

### 2.5 The ceiling — state it in the script docstring

This guard detects **identifier death, not prose truth.** Specifically:

- It fires when a cited identifier stops existing. It cannot detect a claim that is wrong *about a live
  identifier* ("`X` defaults to false" when it defaults to true).
- Once a token is allowlisted as archaeology, a *later* false claim about that same token is invisible.
  `CODE_GRAPH_ENABLED` will be allowlisted on day one (current prose deliberately says it "is read NOWHERE"),
  so this exact token is permanently outside the guard from here on.
- It is a **nudge that forces a look**, in the same family as the I32 docstring's "status accuracy is a
  human responsibility". It is not a proof of registry correctness. Do not advertise it as one.

### 2.6 Mutation test (mandatory)

`revert-the-doc-fix` does **not** work as a mutation: measured, `UNRESOLVED=19` with an identical token list
at both `7cd74ea0` and the current worktree — the guard is red on the *fixed* tree too, because corrected
prose still names the dead token. The mutation must therefore be:

1. Land the baseline allowlist (16 entries) → confirm **green**.
2. Rename `MODEL_QUERY_PREFIX` → `MODEL_QUERY_PREFIX_X` at its three sites
   (`yadgar/_shared/embeddings/embeddings.py:42`, `:287`; `_shared/embeddings/__init__.py:23`;
   `_shared/embeddings/remote_embeddings.py:20`) → confirm **red** with
   `DEAD-CLAIM: ... MODEL_QUERY_PREFIX`.
3. Revert. Record the red output verbatim in the car's PR body.
4. Second mutation for the stale rule: add a bogus allowlist entry for a live token → confirm **red** with
   `STALE`.

**Why this token and not a Settings field.** The mutation must be visible to the *new* guard and to nothing
else, or the evidence is a wall of unrelated red. `MODEL_QUERY_PREFIX` is a plain module constant cited only
in `explanation:`/`wiring:` prose (`CAPABILITY_REGISTRY.md:516`, `:529`) and carried on **no** structured
field — so no other gate can see it. Renaming a Settings field instead (e.g.
`CODE_GRAPH_REFRESH_STOP_INTERVAL`, which sits on CAP-CODEGRAPH-001's `settings:` line) would simultaneously
trip I32 ORPHAN+STALE, I25 three-way-sync and `config_yaml` — proving nothing about this guard. Verified
prose-only: 23 registry tokens are prose-only-and-live; this is the cleanest of them.

---

## 3. Car 2 — internal-route existence guard (D2)

### 3.1 The defect class

A test that mocks an internal HTTP route never verifies the route exists. `yadgar/core/vacuum/__init__.py:889`
POSTs `{yadgar_url}/api/check_invariants`; no such route is registered anywhere. Six tests mock that exact
URL to return 200 (`yadgar/tests/core/test_vacuum.py:507`, `:975`; `test_vacuum_readiness.py:116`, `:236`;
`test_vacuum_e2e.py:311`; `test_vacuum_exit_code.py:123`), and
`test_vacuum.py:1381-1460` even asserts the POST carries a bearer header. `check_invariants` exists only as a
backend admin op (`yadgar/backend/admin_exec/invariants.py`) reachable via the MCP tool — never over HTTP.

### 3.2 Feasibility — measured, not guessed

**Route table is statically enumerable.** 65 routes recovered by AST across both apps: core
`@mcp_server.custom_route("<path>", methods=[...])` (`yadgar/core/server/http.py` and
`yadgar/core/server/routes/*.py`) and backend `@app.<verb>("<path>")`
(`yadgar/backend/embed_service/embed_service*.py`). No dynamic route registration found.

**Two collector strategies were prototyped and measured:**

| Strategy | Collected | Unresolved | Verdict |
|---|---|---|---|
| Regex over `f"{base}/path"` at call sites | 18 | 7 | Thin — misses the `urlopen(Request(url))` shapes. Only 18 of 65 HTTP call sites in non-test code produce a literal this way. |
| AST constant sweep, namespace-prefixed, **unfiltered** | 96 | 30 | Cries wolf. Rejected. |
| AST constant sweep, namespace-prefixed, **filtered** | **75** | **4** | **Ship this.** |

The four filter rules that collapse 30 → 4:

1. reject strings containing a space (kills config help text, log messages, `"/health (API readiness) ..."`)
2. reject strings containing `%` (kills the three `/rerank/%s` circuit-breaker log format strings)
3. strip at `?` (fixes `/hooks/session-context?`, `/hooks/file-changed?path=`, `/hooks/prompt-recall?`, …)
4. reject strings that end in `/` **and** are a strict prefix of a registered route (kills the
   `auth_middleware` prefix-match constants `/api/`, `/hooks/`, `/api/logs/`, `/api/control/action/`)

plus **two-way segment wildcarding**: a `{...}` or `{}` segment on *either* side matches any single segment on
the other. That resolves `/api/control/maintenance/{}` against `/api/control/maintenance/enter`, and
`/api/runtime-config/{}{}` against `/api/runtime-config/{key}`.

**The four survivors, verbatim from the prototype run:**

```
routes=65 collected=75 UNRESOLVED=4
   /api/check_invariants   <- yadgar/core/vacuum/__init__.py:889      ← THE BUG
   /api/generate           <- yadgar/backend/conflict_resolver/conflict_resolver.py:188   (ollama, external)
   /api/search             <- yadgar/core/server/routes/traces.py:172,:311                (Tempo, external)
   /rerank/{}              <- yadgar/backend/ml_client/remote_ml_client.py:70             (false positive)
```

`/rerank/{}` is a genuine false positive worth naming: it is the *label* string passed to `_CircuitBreaker`,
not a URL. The real request is `self._client.post("/rerank", ...)` at `remote_ml_client.py:125`, which
resolves cleanly against `embed_service.py:634`.

**One real bug, two external services, one label false-positive.** Signal-to-noise is 1:3 on a one-time
baseline, and every subsequent unresolved literal is by construction either a new external integration
(one allowlist line) or a real bug. **This is the strongest of the three guards.** The "brittle collector
that cries wolf and gets deleted" risk was the right thing to fear, and it does not materialise at this
repo's scale — say so with the number, and re-measure if the repo grows a lot of new outbound integrations.

### 3.3 Design — `scripts/check_route_literals.py`

- **Route table:** AST-collect decorator-arg path constants for `custom_route` / `get` / `post` / `put` /
  `delete` / `patch` / `route` across non-test `yadgar/**`.
- **Call literals:** AST-collect `Constant(str)` and `JoinedStr`-tail values under non-test `yadgar/**`
  whose first segment is in the namespace prefix set
  `{/api/, /hooks/, /health, /metrics, /admin, /graph, /viz, /recall, /restore, /consolidate, /embed, /rerank, /read_query}`,
  then apply the four filters above.
- **Match:** normalised segment-count + two-way wildcard against the **union** route table.
- **Allowlist** `.route-literal-allowlist.json`: `{path: {rationale, target}}`, rationale ≥40 chars,
  `target` ∈ `{external, label, dynamic}`; **stale entries hard-fail** (an allowlisted path that starts
  resolving must be removed).
- Scope: **non-test code only.** Deliberately narrower than "also scan test mocks" — the production literal
  is the root fact; the six mocks were downstream of it. Narrow beats comprehensive here (the same call the
  code_graph car made today when it scoped its AST guard to one producer rather than globbing a package).

### 3.4 Stated limitations — put these in the docstring

- **Union table ⇒ no per-app targeting.** A Tempo call to `/api/traces/{}` spuriously resolves against our
  own `/api/traces/recent`. Discriminating targets would require a per-call-site manifest, because base-URL
  variable names are *not* reliable: `backend_url` means SurrealDB in `yadgar/core/backup/backup.py:170` and
  `yadgar/core/vacuum/phases.py:109` (both pass `_surreal_headers()`) but is checked as a generic `/health`
  in `yadgar/core/vacuum/__init__.py:1024`. A manifest was considered and rejected as disproportionate for a
  4-survivor baseline — revisit if cross-app misrouting is ever observed in the wild.
- **No method checking.** A GET against a POST-only route passes.
- **No dataflow.** Paths assembled from variables (`p = "/api/" + name`) escape entirely.
- Coverage is the *literal* surface, which the measurement shows is where this bug class lives.

### 3.5 Mutation test (mandatory)

The direction depends on D5. Either:

- **(sequenced-after-vacuum-car)** Guard lands green. Mutation: re-add a
  `httpx.post(f"{url}/api/check_invariants")` line to a throwaway non-test module → confirm **red** → revert.
- **(sequenced-before)** Guard lands red on the live `vacuum/__init__.py:889`, which *is* the demonstration.
  Record it, then the vacuum car's fix turns it green — a cross-car proof. Requires the guard car to merge
  after the vacuum car regardless, or CI is red in between.

Either way the PR body must carry the verbatim red output. Additionally mutate the stale rule: allowlist
`/api/stats` (a live route) → confirm **red**.

---

## 4. Car 3 — e2e lint scan roots (D3)

### 4.1 The six modules outside the scan root

`scripts/check_e2e_assertions.py:28` pins `_E2E_DIR = yadgar/tests/e2e/`. Six `*_e2e*` modules live outside it:

| Module | Guard | Runs in CI? |
|---|---|---|
| `yadgar/tests/core/test_backend_traceparent_e2e.py` | none | yes (`test-core`) |
| `yadgar/tests/core/test_code_graph_e2e.py` | module `pytestmark = skipif(shutil.which("codebase-memory-mcp") is None)` (`:37`) | **no — always skipped** |
| `yadgar/tests/core/test_consolidation_embedded_e2e.py` | none | yes (`test-core`) |
| `yadgar/tests/integration/test_vacuum_e2e.py` | `pytest.mark.integration` (`:39`) | depends on marker selection |
| `yadgar/tests/scripts/test_v5_42_1_gate_verification_e2e.py` | `pytest.mark.integration` (`:145`) | depends |
| `yadgar/tests/scripts/test_v5_42_2_branch_default_e2e.py` | `pytest.mark.integration` (`:144`, `:203`) | depends |

Only `test_code_graph_e2e.py` is both outside the scan root *and* unconditionally skipped. Its skip is
**properly governed** — `yadgar/tests/skip_inventory.json:122-126` sanctions it under
`code-graph-e2e-smoke-01` with a ≥40-char note, and that note itself already records the mitigation:
"the load-bearing CI-VISIBLE coverage is `test_code_graph_cli.py::TestDispatch::test_refresh_reemits_stale_marked_digest_on_fetch_failure`, which needs no binary."
The repo's ADR-0087 skip governance is working. The gap is narrower than "invisible test": it is that
**an acceptance criterion placed only in that module is neither run nor assertion-linted.**

### 4.2 CI presence — the correct picture

`check_e2e_assertions` does reach CI, via `validate.yaml`'s `pre-commit run --all-files` (§1.2). It is not
CI-absent. Note that `yadgar/tests/core/test_tamper_guards.py` imports the script but only exercises
`lint_file()` against `tmp_path` fixtures — it never calls `lint_dir()` over the real tree — so the
`tests/core` suite is *not* a second enforcement path. `validate.yaml` is the only one.

`check_test_weakening.py` has **two** independent scan-root pins: the hook's `files:` filter and its own
hardcoded `yadgar/tests/e2e/.*\.py` regex at `:70-71`. A widening that touches only the `files:` filter leaves
it blind. (Its separate CI-inertness problem is Car 4, §5 — orthogonal, but both cars edit this file, so
serialise them.)

### 4.3 Design

1. **Widen `check_e2e_assertions.py`:** replace the single `_E2E_DIR` with a scan set =
   `yadgar/tests/e2e/**/*.py` ∪ `yadgar/tests/**/*e2e*.py`. Keep `lint_dir()` as the public entry so
   `test_tamper_guards.py` keeps working; add `lint_scope()` alongside.
2. **Widen `check_test_weakening.py` in lockstep:** the internal regex at `:70-71` **and** the hook's
   `files:` filter. Widening one without the other recreates the trigger gap.
3. **Widen both `files:` filters** to `^yadgar/tests/.*\.py$` (or `always_run: true` — see D2 below).
4. **Add a `test_tamper_guards.py` case that calls `lint_scope()` over the REAL tree** and asserts zero
   violations. This is what gives the lint CI presence at all, since `tests/core/` runs in CI. Cheap and
   immediate — independent of Car 4.
5. **Do not** attempt to gate "an acceptance criterion must not be satisfied only by a skipped test". That
   needs a BC↔test mapping the repo does not have (`check_contract_coverage.py` maps BC rows to tests but not
   to skip state), and ADR-0087's skip inventory already forces a ≥40-char justification per skip. Out of
   scope; note it as a follow-up if criterion-orphaning recurs.

**Measured: the widening lands green.** Running the current `lint_file()` over all six out-of-root modules
returns **0 violations**. No fix-up work; the car is a pure mechanical widening.

### 4.4 Mutation test (mandatory)

1. Append an assertion-free `def test_mutation_probe(): pass` to
   `yadgar/tests/core/test_backend_traceparent_e2e.py` → run widened lint → confirm **red** naming that file
   and line. (This file is *outside* the old scan root, so the same probe against the unwidened lint must be
   **green** — run both and record both.)
2. Revert.
3. Same probe shape for `check_test_weakening`: stage a diff removing an `assert` from
   `yadgar/tests/core/test_consolidation_embedded_e2e.py` → confirm the widened script flags it and the
   unwidened one does not.

---

## 5. Car 4 — `check_test_weakening` is a structural no-op in CI

### 5.1 The defect

`scripts/check_test_weakening.py:137` sources its entire input from `git diff --cached`. A CI checkout has an
empty index — nothing is staged — so `diff_text` is `""`, `check_diff` finds nothing, and `main()` prints
`test-weakening guard OK.` and exits 0.

**Reproduced empirically**, not inferred — run on this train branch (20 commits ahead of `origin/master`,
nothing staged, i.e. exactly the CI checkout condition):

```
$ git diff --cached --stat      # empty
$ python3 scripts/check_test_weakening.py
test-weakening guard OK.
exit=0
```

It runs in CI (via `validate.yaml`'s `pre-commit run --all-files`) and **cannot fail there, ever, regardless
of what the PR contains.** Layer 3's tamper protection exists only for contributors with hooks installed —
precisely the population least likely to be tampering.

This is the purest instance of the class in the plan: correct trigger, correct scope, correct wiring, and an
assertion that is structurally incapable of firing where it matters.

### 5.2 Design

Give the script a branch-diff mode, exactly mirroring ADR-0080's `check_backend_bump` contract:

- Baseline = `git merge-base origin/master HEAD`; diff = `git diff <base>...HEAD` ∪ `git diff --cached`.
- One pure `check_diff()` fed from the same inputs in both modes, so local and CI return the same verdict for
  the same repo state — ADR-0080's parity contract, restated.
- Fall back to the current staged-only behaviour when `origin/master` is unreachable (identical fail-open to
  `check_backend_bump`).
- `_green_count_head()` / `_green_count_staged()` need the same baseline treatment — compare the contract's ✅
  count at merge-base vs HEAD, not HEAD vs index.
- Hook becomes `always_run: true` (branch semantics require every commit checked — ADR-0080 again).

**This is the one place in the plan where ADR-0080's mechanism generalises exactly.** The brief asked whether
it does; the answer is yes, but only for D4, and not for D1 (§1.1).

Same car, since it is the same file: widen the internal `yadgar/tests/e2e/.*\.py` regex per §4.3 item 2.

### 5.3 Mutation test (mandatory)

1. On a scratch branch, delete an `assert` from a file in the widened scope, **commit it** (do not leave it
   staged), then run the script → confirm **red** under branch-diff mode. The pre-fix script is green on the
   same state (nothing staged) — run both and record both. That contrast *is* the proof.
2. Push the scratch branch and confirm the `validate` job goes red in CI. Record the run URL.
3. Revert.

> The mutation needs pre-commit not to block the intermediate commit. Use `ALLOW_TEST_WEAKEN=1` (the script's
> own documented bypass, `:40`) or `SKIP=check-test-weakening`. **Never `--no-verify`.**

---

## 6. Acceptance criteria

**Car 1 — I32 prose liveness**
- [unit] `scripts/check_registry_prose_liveness.py` exits 0 on the current tree with the baseline allowlist.
- [unit] New test module asserts: a fixture registry citing a token present only in a docstring is flagged;
  the same token in executable code is not; an allowlist entry with a <40-char rationale hard-fails; a stale
  allowlist entry (token alive again) hard-fails.
- [manual] Mutation §2.6 steps 2–4 executed, red output pasted in the PR body.
- [unit] `check_capability_coverage.py` still exits 0 (unchanged behaviour — the new check is a separate
  script).
- [manual] Hook runs in <2s on this tree (`time` output in PR body).

**Car 2 — route-literal guard**
- [unit] `scripts/check_route_literals.py` exits 0 with the baseline allowlist (`/api/generate`,
  `/api/search`, `/rerank/{}`, plus `/api/check_invariants` only if D5 chooses that path).
- [unit] Test module asserts: route table recovers ≥65 paths; the four filter rules each drop their
  documented noise class; two-way wildcard resolves `/api/runtime-config/{}{}`; an unresolved namespace path
  is flagged; a stale allowlist entry hard-fails.
- [manual] Mutation §3.5 executed, red output pasted in the PR body.

**Car 3 — e2e lint scan roots**
- [unit] Widened `check_e2e_assertions.py` exits 0 over the full scan set (verified: 0 violations today).
- [unit] `test_tamper_guards.py` gains a case invoking `lint_scope()` over the real repo, asserting 0
  violations — this is the CI hook.
- [manual] Mutation §4.4 executed with **both** the widened and unwidened runs recorded (proving the old
  scan root was blind).

**Car 4 — `check_test_weakening` branch-diff mode**
- [unit] Branch-diff mode implemented; one pure `check_diff()` shared by both modes; fail-open when
  `origin/master` is unreachable.
- [unit] Test module asserts: a committed (not staged) assert-removal on a branch is flagged; the same state
  is *not* flagged by staged-only mode; `ALLOW_TEST_WEAKEN=1` still bypasses; unreachable `origin/master`
  falls back without raising.
- [unit] Internal `yadgar/tests/e2e/` regex widened in lockstep with Car 3's scan set.
- [e2e] Mutation §5.3 step 2: `validate` job goes red on the scratch branch; run URL in the PR body.
- [manual] Mutation §5.3 steps 1 and 3 executed; both outputs pasted in the PR body.
- [unit] `docs/ARCHITECTURE_INVARIANTS.md` enforcement-lints note corrected to name `validate.yaml` as the
  path by which non-`invariant-checks` lints reach CI (§1.2); wiki mirror updated.

**All cars**
- [unit] `ruff`, `import-linter`, I13/I25/I29/I30/I32/I33 all green.
- [manual] `check_versions` green; version bumped per the `verify-version-bump` gate (core only — none of
  these cars touch `yadgar/backend/**`, so `backend_version` must **not** move).

---

## 7. Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | **Car 2's guard is red on the current tree** until `car/vacuum-reclaim` merges. | D5 decides. Do not silently allowlist `/api/check_invariants` — that reintroduces exactly the "allowlist absorbs the signal" antipattern this plan exists to fight. |
| R2 | Car 1's allowlist becomes a dumping ground; every future rot is allowlisted instead of fixed. | ≥40-char rationale + hard stale-check, both proven mechanisms in this repo (I30). Accept that this is mitigation, not elimination. |
| R3 | Docstring prose rots identically to registry prose (`code_graph/config.py:19-20` carried the same false claim) and Car 1 does not gate it. | Out of scope, named explicitly. Gating docstrings would multiply the claim surface by ~100× with no measurement behind it. Revisit only if docstring rot causes a second incident. |
| R4 | Car 3's widened scan root catches a future test that legitimately has no assertion. | The `# tamper-lint: no-assert <reason>` escape hatch already exists (`check_e2e_assertions.py:12-14`). |
| R5 | Car 4 makes a previously-inert CI gate live — historical assert-removals already on the branch become visible and turn CI red on first run. | Run the branch-diff mode against the current train **before** wiring, and fix or `ALLOW_TEST_WEAKEN`-document findings in the same car. Likely small (the guard has been live at commit time all along) but must be measured, not assumed. |
| R6 | Cars 3 and 4 both edit `scripts/check_test_weakening.py` and `.pre-commit-config.yaml`. | Serialise: Car 3 then Car 4 (Car 4 needs Car 3's widened scan set to exist). This is the main reason they are one plan. |
| R7 | Car 2's union route table hides cross-app misrouting (Tempo `/api/traces/{}` resolves against our `/api/traces/recent`). | Documented limitation, not fixed. A per-call-site manifest is the fix if this ever bites; base-URL variable names are proven unreliable (§3.4) so nothing cheaper works. |

---

## 8. Open decisions

**D1 — Car 1 scope: registry only, or registry + `BEHAVIOR_CONTRACT.md`?**
The contract doc has the same prose-claim surface and is already parsed by `check_contract_coverage.py`.
Extending doubles the baseline allowlist work, unmeasured. *Recommendation: registry only for v1; measure the
contract doc's unresolved count before deciding.* **Needs a call.**

**D2 — Car 3 hook triggers: widen `files:` to `^yadgar/tests/.*\.py$`, or go `always_run: true`?**
`always_run` is strictly safer (the checker reads the whole scan set regardless of what is staged) but adds
~0.3s to every commit for two hooks. `files:` widening is a smaller diff but re-creates a trigger/read-set
mismatch if the scan set ever grows again. *Recommendation: `always_run: true` for `check_e2e_assertions`
(cheap whole-tree AST); leave `check_test_weakening` on `files:` since it is diff-driven and genuinely only
cares about staged test files.* **Needs a call.**

**D3 — Car 4: fix `check_test_weakening`'s CI-inertness, or accept it and document?**
The layer-3 guard has value at commit time and the branch-diff rewrite is a real (if small) change to a
tamper-protection script — a class of code where a subtle bug is worse than a known gap.
*Recommendation: fix it — the mechanism is already proven in `check_backend_bump`, so this is a port, not an
invention.* **Needs a call.**

**D4 — Ship order if only some cars land.**
*Recommendation: Car 2 first (only car with a proven live production bug in its baseline), then Car 3
(mechanical, lands green), then Car 4 (depends on Car 3), then Car 1 (largest allowlist work, softest
guarantee).* Note this contradicts an earlier draft of this plan that put Car 4 first on a since-corrected
premise (§1.2). **Needs a call.**

**D5 — Car 2 sequencing vs `car/vacuum-reclaim`.** Three options, none free:
  1. **Sequence after** the vacuum car. Clean, guard lands green, adds a hard dependency between trains.
  2. **Ship with `/api/check_invariants` allowlisted**, removal as a follow-up. Reintroduces the antipattern.
  3. **Ship script + tests now, wire the hook/CI step in a follow-up.** Guard exists but does not gate.
  *Recommendation: option 1.* This also fixes the mutation-test direction (§3.5). **Needs a call — this is
  the blocking decision for Car 2.**

**D6 — `check_secret_gate.py:168` uses `tools_dir.glob("*.py")` (non-recursive) while
`check_capability_coverage.py:123` uses `rglob("*.py")` over the same directory.**
Confirmed **latent, not live**: `find yadgar/core/server/tools -mindepth 2 -name "*.py"` returns nothing
today, so I26 currently sees every tool. The day someone adds `tools/<subdir>/foo.py`, I32 catalogues it and
I26's secret gate silently does not — the same defect class, pre-loaded. One-word fix (`glob` → `rglob`).
Fold into Car 1 (which already touches the enumeration surface) or file separately? **Needs a call.**

---

## 9. Non-goals

- The vacuum `/api/check_invariants` bug itself (`car/vacuum-reclaim` owns it).
- A meta-guard over `.pre-commit-config.yaml` — evidence in §1.3 says no.
- Docstring prose liveness (R3).
- A BC↔test↔skip-state acceptance-criterion mapping (§4.3 item 5).
- Per-call-site target manifest for Car 2 (§3.4) — disproportionate at a 4-survivor baseline.
