> ARCHIVED 2026-07-09 — CLOSED; issue premise refuted, warning already wired on all transports

# PLAN — COMET-dormant startup warning reachability (#25)

Created 2026-06-25 (improvement-train #29, group C). theme: observability / startup /
COMET. priority: low.

## STATUS: LIKELY ALREADY CLOSED — needs a user decision, not a build

The #25 ticket premise — *"`warn_comet_dormant` at lifecycle.py:749 not reached by the
`yadgar --transport streamable-http` entrypoint"* — is **REFUTED by current code** (the
COMET-retire-dormant ship wired it on a path all transports take). The remaining
question is observability-routing, which is a user call, not a code defect. Do not
build a "wire it up" fix; it is already wired.

## What the code actually does (verified 2026-06-25)

- `warn_comet_dormant(settings)` is **defined** at `yadgar/config_registry.py:390`
  (NOT lifecycle.py — the ticket's "lifecycle.py:749" is the CALL site). Logs exactly
  ONE `logger.warning(...)` when `not settings.COMET_ENRICHMENT_ENABLED` (the new
  default; `config_registry.py` ConfigEntry default `"false"`). Idempotent via a
  module-level once-flag. BC-EN2b.
- **Call site:** `yadgar/server/lifecycle.py:749`, inside `def main(...)` (line 693),
  unconditional, right after `init_engines(...)` and `emit_startup_config_log()`,
  wrapped in a `try/except Exception: logger.debug(...)` (line 750).
- **Entrypoint trace for `yadgar --transport streamable-http`:**
  `pyproject.toml` `yadgar = "yadgar.__main__:cli"` → `__main__.cli()` routes the
  no-subcommand case to `from yadgar.server import main` (`__main__.py:155`) →
  `server/__init__.py` re-exports `lifecycle.main` → `lifecycle.main(transport=...)`
  runs line 749 **before** the transport is started. Container path is identical:
  `entrypoint.sh: exec yadgar --transport streamable-http`.
- **Conclusion:** the warning fires on stdio, sse, AND streamable-http. It is NOT
  transport-gated. Test `yadgar/tests/test_comet_dormant_warning.py` covers the warn
  function directly (it does not cover the lifespan wiring — see "if anything ships").

## The only real silencers (these are the actual residual)

1. **The `except Exception` swallow** (`lifecycle.py:750`) — a failure inside
   `emit_startup_config_log()` / `_set_config_gauges()` / `warn_comet_dormant()` is
   downgraded to a `logger.debug` and the warning is lost. Broad catch.
2. **Log-level gating** — `YADGAR_CORE_LOG_LEVEL` above `WARNING` suppresses it.
3. **Routing** — the warning lands in the **server/daemon process logs**
   (`docker logs yadgar-core`), NOT the MCP client (Claude Code) console. "Silent"
   only from the operator-at-the-client viewpoint.

## Decision for the user (FLAG — do not invent)

#25 reduces to: **is a server-log WARNING sufficient, or should the dormant state be
surfaced client-visibly?** Options, smallest first:
- **(A) Close #25 as already-satisfied.** The warning fires + `/admin/config`
  surfaces `COMET_ENRICHMENT_ENABLED` (per the comet-retire ship). Server-log is the
  conventional place for startup state. Recommended unless the user wants more.
- **(B) Narrow the `except` + add a wiring test.** If the user wants robustness:
  don't let the broad `except` swallow the BC-EN2b warning — move `warn_comet_dormant`
  out of the shared try, or catch narrowly. Add a test asserting `main()` (mocked
  transport) emits the warning, closing the lifespan-wiring coverage gap.
- **(C) Surface client-visibly.** Add the dormant note to `project_brief` /
  startup-banner / a one-shot MCP notification. Larger; only if server-log is deemed
  insufficient.

## If anything ships (TDD outline for option B)
- `test_main_emits_comet_dormant_warning` — call `lifecycle.main` with mocked
  `init_engines`/transport + COMET disabled; assert exactly one WARNING emitted (red
  if the broad `except` swallows an induced failure; green after narrowing).
- Keep `test_comet_dormant_warning.py` green.

## Config / contracts
- BC-EN2b ("COMET disabled → config reports disabled + exactly ONE startup warning")
  is the governing contract — already implemented per CHANGELOG `[Unreleased]`. Option
  B hardens its delivery; no new BC.
- No I25 change.

## Risk
- Minimal. The only real risk is option B over-narrowing the `except` and letting an
  unrelated startup-config failure become fatal — keep the narrowing surgical to the
  warn call.
