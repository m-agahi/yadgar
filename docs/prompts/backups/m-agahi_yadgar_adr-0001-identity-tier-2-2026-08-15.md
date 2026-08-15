<!-- VERBATIM BACKUP of ADR ledger row id=1 + its wiki body page.
     Row: project_id=m-agahi/yadgar, status=accepted, decided_on=2026-08-15,
          body_slug=m-agahi_yadgar_adr-0001, supersedes=none,
          subsystem=(empty), tier=(empty).
     Page: wiki id 7828, page_type=adr, slug=m-agahi_yadgar_adr-0001,
           directory_context=/home/max/git/yadgar,
           tags=[adr, decisions, adr-status:accepted, adr-0001],
           created_at/updated_at 2026-08-15T16:55:02.200697+00:00.
     Captured 2026-08-15 before any attempt to drop the row / re-align ADR
     numbering. This row occupies AUTO_INCREMENT id=1, which historical
     ADR-0001 needs. -->

# ADR-0001: Identity tier 2 resolves by static header (per-project MCP config) or hook-authored directory map (global config)

- status: accepted
- date: 2026-08-15
- context: Every wiki/ADR write without an explicit project= raised unresolved_project. An earlier wiki page recorded tier 2 as unimplementable ("no shared key between the SessionStart hook and MCP"); the user challenged that, correctly. Measurement showed the real cause: Car B built the whole consumer side (NoncePool, POST /session_bind, SessionBindMiddleware, tier-2 ContextVar) but (a) nothing ever mints a nonce — zero session_bind hits in the live daemon log — and (b) an initialize against the running daemon returns NO Mcp-Session-Id at all, because _startup.py sets stateless_http=True for streamable-http deliberately, so daemon restarts stay transparent. Both readers gated on that session id before reading anything else, so the X-Yadgar-Project-Id header they already knew how to read was unreachable.
- decision: Tier 2 resolves by two routes, chosen by how the MCP client is configured, and the session-id precondition is removed from both readers. Per-project mcpServers entry: a static X-Yadgar-Project-Id header, delivered exactly as Authorization already is. ONE global mcpServers entry (the user's setup, and the common one): a hook-authored directory -> project_id table at $YADGAR_DATA_DIR/session_projects.json, written atomically by the SessionStart hook and looked up by the daemon using the caller's directory argument. Precedence: explicit project= > header > directory map > raise.
- rationale: With one global entry every MCP request is identical on the wire and there is no session id, so directory is the only per-call signal that varies. The table makes it usable without violating ADR-0227, because it is a LOOKUP, not a derivation: the hook mints host-side where the working tree exists and registers the pair, and the daemon only asks whether that exact path was registered. An unregistered directory returns None and the caller fails loud exactly as before, so no key is manufactured from a path. It reintroduces no sticky state (the stated reason auto-bind was rejected) and grants no authority a caller lacked, since project= was always passable.
- alternatives: Wire the nonce producer only — rejected: measurement showed it cannot work, since stateless mode issues no Mcp-Session-Id for the returned token to travel in. Turn off stateless_http to get session ids — rejected: it exists deliberately for restart transparency, and the hook still could not learn the id. Amend ADR-0227 to require project= forever — rejected: it leaves the wall permanent. Per-project config only — rejected: the user runs one global entry across 29 projects and asked for both to work.
- consequences: A new _shared/runtime/session_map.py takes directory as a lookup key, which needed allowlist entries in BOTH ADR-0225 enforcers (the pre-commit lint file and the in-test _ALLOWLIST) — the duplication is itself a trap worth its own car. Identity only resolves after the SessionStart hook has run under the new build, so an already-open session keeps failing until restart. The table is capped at 512 entries, oldest first. An instance can still name another registered directory and read that project, which is the same exposure explicit project= already carries.
- revisit_trigger: The wire gains a real per-session identity (an upstream Claude Code per-session header, or stateless_http being turned off for another reason) — at that point the directory map can be deleted in favour of a genuine session key.
- supersedes: none

## Context

Every wiki/ADR write without an explicit project= raised unresolved_project. An earlier wiki page recorded tier 2 as unimplementable ("no shared key between the SessionStart hook and MCP"); the user challenged that, correctly. Measurement showed the real cause: Car B built the whole consumer side (NoncePool, POST /session_bind, SessionBindMiddleware, tier-2 ContextVar) but (a) nothing ever mints a nonce — zero session_bind hits in the live daemon log — and (b) an initialize against the running daemon returns NO Mcp-Session-Id at all, because _startup.py sets stateless_http=True for streamable-http deliberately, so daemon restarts stay transparent. Both readers gated on that session id before reading anything else, so the X-Yadgar-Project-Id header they already knew how to read was unreachable.

## Decision

Tier 2 resolves by two routes, chosen by how the MCP client is configured, and the session-id precondition is removed from both readers. Per-project mcpServers entry: a static X-Yadgar-Project-Id header, delivered exactly as Authorization already is. ONE global mcpServers entry (the user's setup, and the common one): a hook-authored directory -> project_id table at $YADGAR_DATA_DIR/session_projects.json, written atomically by the SessionStart hook and looked up by the daemon using the caller's directory argument. Precedence: explicit project= > header > directory map > raise.

## Consequences

A new _shared/runtime/session_map.py takes directory as a lookup key, which needed allowlist entries in BOTH ADR-0225 enforcers (the pre-commit lint file and the in-test _ALLOWLIST) — the duplication is itself a trap worth its own car. Identity only resolves after the SessionStart hook has run under the new build, so an already-open session keeps failing until restart. The table is capped at 512 entries, oldest first. An instance can still name another registered directory and read that project, which is the same exposure explicit project= already carries.
