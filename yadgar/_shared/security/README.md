# `_shared/security/` — write-path security

- `secrets.py` — I26 secret gate: `gate_or_reject()` / `check_secrets()`.
  EVERY write tool must call it (`scripts/check_secret_gate.py` enforces).
- `allowlist.py` — sanctioned-secret allowlist + I28 audit-trail invariant
  (`scripts/check_allowlist_audit.py`).
- `enforcement.py` — enforcement-relaxed counters.

Changing gate behavior requires the I26/I28 lints green; the audit write is
internal to `gate_or_reject()` — do not "optimize" it out.
