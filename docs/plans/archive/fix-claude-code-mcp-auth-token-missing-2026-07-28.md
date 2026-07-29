> ARCHIVED 2026-07-29 — executing on fix/claude-code-mcp-auth-token, ships with this PR.
> Implemented: Option A (§4.1) in full — A1 (`resolve_mcp_auth_token()`), A2 (wired into
> `cli/install.py:cmd_install` + `mcp_register.register_mcp_for_claude_code`; `setup.py`'s
> `_existing_secrets_token` refactored to delegate), A3 (OD-1 resolved: loud-warn, non-fatal).
> AC-1..AC-4 regression tests added (AC-5 is the manual fresh-VM check, tracked separately).
> CHANGELOG `[Unreleased]` entry added. OD-2/OD-3 remain open (tracked, not blocking).

# Fix: `yadgar install --client claude-code` writes a headerless (unauthenticated) MCP entry

**Task:** #71 (harness) — fresh-VM QA bug, 2026-07-28.
**Status:** INVESTIGATION + PLAN — no implementation. Review-only.
**Builds on:** ADR-0144 (multi-client framework, `clients/` registry), ADR-0161 (`yadgar setup` claude-code MCP auto-register).
**Related files:** `yadgar/core/cli/install.py`, `yadgar/core/install/clients/mcp_register.py`, `yadgar/core/install/clients/install.py`, `yadgar/core/cli/setup.py`, `yadgar/_shared/paths/paths.py`.
**Date:** 2026-07-28

---

## 1. Symptom (confirmed live on a Debian 13 fresh-install VM, yadgar 5.167.0)

`yadgar install --client claude-code` on a clean machine writes into `~/.claude.json`:

```json
"yadgar": { "type": "streamable-http", "url": "http://127.0.0.1:8765/mcp" }
```

— no `headers` / `Authorization`. The daemon runs with `YADGAR_REQUIRE_AUTH=1`; an
unauthenticated `initialize` returns 401. Net effect: the documented fresh
claude-code install produces a client that cannot authenticate to its own daemon.

Two paths do NOT reproduce it (this is the tell):

- `yadgar install --client claude-code --print` correctly SHOWS a `Bearer …` header.
- The **opencode** writer correctly writes a header on the real write.

---

## 2. Root cause (verified against source at `origin/master` = `805f5e4b`)

### 2.1 The write path resolves the token from `os.environ` ONLY

`cli/install.py:97` (`cmd_install`):

```python
token = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "").strip()
```

That is the **only** token source for the `yadgar install` write path. The bearer
token's source of truth on a real install is `~/.config/yadgar/secrets.env`
(`paths.py:90` `_secrets_env_path` → `SECRETS_ENV_PATH`; line
`YADGAR_MCP_AUTH_TOKEN=…`, written by `yadgar setup`). **The daemon** sources
`secrets.env` into its own process env (`daemon.py:278` — "The daemon process has
already sourced secrets.env via its own env"), but the **interactive shell** where
a user runs `yadgar install` does not. On a fresh VM the env var is unset in that
shell → `token = ""`.

`register_mcp_for_claude_code` (`mcp_register.py:274`) — the
`yadgar daemon configure-mcp` back-compat wrapper — has the identical defect:
`token = os.environ.get(_TOKEN_ENV_VAR, "").strip()`, env-only.

### 2.2 `BEARER_LITERAL` drops the header when the token is empty

claude-code is `McpAuth.BEARER_LITERAL` (`registry.py:56`). `_resolve_auth_header`
(`mcp_register.py:143-144`):

```python
if descriptor.mcp_auth is McpAuth.BEARER_LITERAL:
    return f"Bearer {token}" if token else None
```

Empty token → `None` → `_serialize_streamable_http_type` (`mcp_register.py:61-62`)
omits the `headers` key entirely. Result: the headerless entry above.

### 2.3 Why the two "working" paths mask the bug

| Path | Token dependence | Header written? |
|------|------------------|-----------------|
| `install …claude-code` (real write) | `BEARER_LITERAL`, token from `os.environ` only → `""` on fresh VM | **NO** (bug) |
| `install …claude-code --print` | `_render_mcp_fragment` (`install.py:120-123`) forces `Bearer ${YADGAR_MCP_AUTH_TOKEN}` **unconditionally** for any non-NONE auth, ignoring the token value | YES (always) |
| opencode (real write) | `BEARER_ENVREF` (`registry.py:322`) → `_resolve_auth_header` returns `Bearer ${YADGAR_MCP_AUTH_TOKEN}` **unconditionally**, token-independent; opencode resolves `${…}` (env-substitution in `opencode.json`) at load | YES (always) |
| `yadgar setup` (real write) | reads token from `secrets.env` (`setup.py:178` `_existing_secrets_token`) and passes it explicitly to `register_mcp(..., token=mcp_token)` | YES (correct) |

The asymmetry is precise: **`--print` and opencode both emit the env-ref
unconditionally (token-independent), so neither exercises the write-path token
resolution that is actually broken.** `yadgar setup` is the one real write path that
gets it right — because it resolves the token from `secrets.env`, which
`yadgar install` does not.

### 2.5 secrets.env provably held a token at the moment QA reproduced this

The observed repro is deductive proof that the `secrets.env` fallback (§4.1) would
have fixed it: QA confirmed the daemon was **live and returning 401** to an
unauthenticated `initialize`. The daemon's only auth-token source is `secrets.env`
(`daemon.py:278` — the daemon sources `secrets.env` into its own env; auth
middleware `auth_middleware.py:114` compares against `YADGAR_MCP_AUTH_TOKEN`). A
daemon enforcing a bearer token therefore necessarily had a populated `secrets.env`
with `YADGAR_MCP_AUTH_TOKEN=…` on disk at repro time. Documented flow confirms the
ordering: `yadgar setup` mints `secrets.env` (README §post-install), and
`yadgar install --client <name>` is the separate multi-client wiring command
(README) — so `secrets.env` predates a standalone `yadgar install`. **Option A
resolves the token in exactly the scenario QA hit.**

### 2.4 The framing "match opencode / --print" needs one correction

Matching opencode/`--print` *at the shape level* would mean flipping claude-code to
`BEARER_ENVREF` (write `Bearer ${YADGAR_MCP_AUTH_TOKEN}` literally). That is
**explicitly unverified and risky** per the standing D5 TODO
(`mcp_register.py:17-23`): whether Claude Code expands `${…}` inside
`headers.Authorization` at session load "is not clearly documented; primary-source
verification was attempted but inconclusive." CC's `~/.claude.json` is observed to
hold a *literal* token today, which is why CC was deliberately kept on
`BEARER_LITERAL`. If CC does **not** expand env-refs in headers, an env-ref write
would send the literal string `${YADGAR_MCP_AUTH_TOKEN}` as the bearer → still 401,
i.e. re-break the same symptom with a different value. The correct lesson from
opencode is not "write env-ref" but "**always emit a working `Authorization`
header**" — for opencode that is env-ref (it expands), for CC that must be the
real literal token, resolved the way `yadgar setup` already resolves it.

---

## 3. Why it slipped through (test gap — verified)

`yadgar/tests/clients/test_mcp_register.py` tests only the **serializer** with a
token injected directly:

- `test_streamable_http_type_entry` (:79) passes `token=_TOKEN` and asserts the
  literal header — always green, because it hand-feeds the token.
- `test_bearer_literal_without_token_no_headers` (:152) passes `token=""` and
  asserts `"headers" not in entry` — i.e. it **enshrines the buggy omission as
  correct behavior at the serializer layer.** (Fine as a pure serializer contract,
  but it means the real defect lives one layer up and no test covers that layer.)

**No test exercises token *resolution*** — i.e. runs `cmd_install` /
`register_mcp_for_claude_code` with an empty process env + a populated
`secrets.env`, then reads back the written `~/.claude.json` and asserts the
`Authorization` header is present. That is exactly the seam the bug lives in, and
exactly why a mock/unit-level suite stayed green while the real fresh install
shipped broken. The regression test MUST be at that level (§6, AC-2).

---

## 4. Proposed fix

### 4.1 Recommended — Option A: resolve the literal token from `secrets.env` (root cause)

Make the claude-code write path resolve the token exactly as `yadgar setup`
already does: env var first, then fall back to parsing `secrets.env`.

**A1 — Extract a shared token resolver.** Promote the parse logic currently private
in `setup.py` (`_existing_secrets_token`, :178-193) into a shared, reusable helper
— e.g. `resolve_mcp_auth_token()` in a small module such as
`yadgar/core/install/clients/mcp_register.py` or a `_shared` secrets helper — with
resolution order:

1. `os.environ["YADGAR_MCP_AUTH_TOKEN"]` (stripped, if non-empty)
2. else parse `YADGAR_MCP_AUTH_TOKEN=` from `SECRETS_ENV_PATH`
   (`paths.SECRETS_ENV_PATH`, honoring the `$YADGAR_SECRETS_ENV_FILE` override)
3. else `""` (no token available)

Never raises; empty return means "no token" (same contract as
`_existing_secrets_token`).

**A2 — Use it at the write-path entrypoints:**
- `cli/install.py:97` — replace the bare `os.environ.get(...)` with
  `resolve_mcp_auth_token()`.
- `mcp_register.py:274` (`register_mcp_for_claude_code`) — same.
- `setup.py` — refactor `_existing_secrets_token` to delegate to the shared helper
  (behavior-preserving; keeps setup's proven path identical).

**A3 — Decide the empty-token behavior (see Open Decision OD-1).** When auth is
required and no token can be resolved at all, the current `BEARER_LITERAL` silent
omission is the worst outcome (a silently-401 entry). Options: (a) keep writing
headerless but print a loud warning (matching `setup.py`'s `_register_claude_code_mcp`
skip-with-message at :221-228), or (b) fail the command with a clear
"run `yadgar setup` to mint a token" message. Recommend loud-warn, non-fatal,
consistent with setup.

**Scope:** token-resolution only. Do **not** touch the serializers, the
descriptor schema, or the `--print` env-ref contract (`--print` must keep emitting
env-ref — never a literal secret to stdout/nix store; `install.py:26-27`).

**Why A is correct and safe:** it reuses behavior already proven correct in
`yadgar setup` (ADR-0161), keeps CC on the observed-working `BEARER_LITERAL`
literal-token shape, and needs no unverified assumption about CC env-ref expansion.

### 4.2 Rejected — Option B: flip claude-code to `BEARER_ENVREF`

Would make the header token-independent (always `Bearer ${YADGAR_MCP_AUTH_TOKEN}`),
structurally matching opencode. **Rejected as the primary fix** because CC env-ref
expansion in `headers.Authorization` is unverified (§2.4 / D5 TODO). If CC does not
expand it, this re-breaks the identical symptom. Revisit only if/when a
primary-source check confirms CC expands env-refs in MCP headers (then B is simpler
and A's `secrets.env` fallback becomes belt-and-suspenders). Tracked as OD-2.

### 4.3 Not in scope

Nix `#67` provisioning, the `--print` contract, other clients' auth modes, and the
daemon's own auth middleware are all correct and untouched.

---

## 5. Cars / phases

- **Car 1 — shared resolver + wiring (core).** A1 + A2 + A3. Single small module +
  three call-site edits. `ruff` + `mypy` clean.
- **Car 2 — regression tests.** AC-1..AC-4 below. Written RED first (assert header
  present against a checkout that still has the bug → fails), then Car 1 turns them
  green.
- **Car 3 — docs / CHANGELOG.** `[Unreleased]` entry; note the `configure-mcp` and
  `install` parity fix. No ADR needed (bug fix within ADR-0144/0161 envelope);
  add a one-line note to ADR-0161 if the reviewer wants the provenance.

Single PR, Cars ordered 2→1 (test-first) then 3.

---

## 6. Acceptance criteria

- **AC-1 [unit]** `resolve_mcp_auth_token()`: env set → returns env value; env
  empty + `secrets.env` has the line → returns the file value; both absent →
  `""`; respects `$YADGAR_SECRETS_ENV_FILE` override; never raises on missing/
  malformed file.
- **AC-2 [e2e] — the test that would have caught this.** With `YADGAR_MCP_AUTH_TOKEN`
  **absent** from the environment and a temp `secrets.env`
  (`$YADGAR_SECRETS_ENV_FILE` → tmp file) containing a known token, run the **real**
  `cmd_install` code path (`--client claude-code`, `--mcp`, `home_dir`/`HOME` →
  tmp), then **read back the written `~/.claude.json`** and assert
  `mcpServers.yadgar.headers.Authorization == "Bearer <known-token>"`. No mocking of
  the serializer or of `register_mcp`. Mirrors the exact fresh-VM condition (env
  unset, secrets.env present).
- **AC-3 [e2e]** Same for `register_mcp_for_claude_code` (the `configure-mcp`
  back-compat path) — env unset + secrets.env present → header written.
- **AC-4 [unit/regression]** `--print` still emits the **env-ref** (never the
  literal token), for both claude-code and opencode — guards against a fix that
  leaks the secret into dry-run output.
- **AC-5 [manual]** On a fresh VM (or a clean container), following the documented
  order: `yadgar setup` (mints `secrets.env`), then — in a shell that has NOT
  sourced `secrets.env` — `yadgar install --client claude-code` (the documented
  wiring command; defaults to mcp+rules). Confirm `~/.claude.json` has the
  `Authorization` header and an `initialize` call succeeds (no 401). This is the
  original repro; pre-fix it writes the headerless entry, post-fix it writes the
  header.

---

## 7. Risks

- **R1 — secrets.env format drift.** The resolver keys on the literal
  `YADGAR_MCP_AUTH_TOKEN=` line prefix (same as `_existing_secrets_token` today). A
  future secrets.env format change would silently return `""`. Mitigation: reuse the
  single shared parser so setup + install drift together, not apart; AC-1 pins the
  contract.
- **R2 — token in `~/.claude.json` on disk.** A1 writes the literal token to
  `~/.claude.json` (as it does today, and as `yadgar setup` already does). No new
  exposure surface; `--print`/nix stay env-ref (AC-4). Note only.
- **R3 — precedence surprise.** If a user deliberately exported a *different*
  `YADGAR_MCP_AUTH_TOKEN` than the one in secrets.env, env-first preserves today's
  behavior (env wins). Intentional; documented in the resolver docstring.
- **R4 — partial fix if OD-1 chooses "warn + headerless".** Then a token-less
  machine still writes a broken entry (but loudly). Acceptable as a fallback; the
  common fresh-VM case (secrets.env present) is fully fixed regardless.

---

## 8. Open decisions for the user

- **OD-1 — empty-token behavior when NO token resolvable at all** (no env, no
  secrets.env line): loud-warn + skip/headerless (matches `setup.py`
  `_register_claude_code_mcp`), or hard-fail the command with a "run `yadgar setup`"
  message? Recommend loud-warn, non-fatal.
- **OD-2 — verify CC env-ref expansion?** Worth a one-shot primary-source /
  empirical check of whether Claude Code expands `${YADGAR_MCP_AUTH_TOKEN}` inside
  `headers.Authorization`. If YES, Option B (flip to `BEARER_ENVREF`) becomes the
  simpler long-term shape and the D5 TODO can be closed; Option A's fallback stays
  as defense-in-depth. If NO, Option A is the only correct fix and the D5 TODO
  should be marked "confirmed: CC does not expand — stays literal." Independent of
  shipping Option A now.
- **OD-3 — scope of parity fix.** Fix only claude-code + its `configure-mcp`
  wrapper (both `BEARER_LITERAL`), or audit every write-path entrypoint for the
  same env-only token resolution? Recommend: fix both `BEARER_LITERAL` entrypoints
  now (they are the only literal-auth clients); env-ref clients are already
  token-independent and unaffected.
