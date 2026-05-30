# PLAN — v5.13.0: Secret-gate pattern context-awareness + allowlist (false-positive reduction)

**Renumbered:** v5.11.0 → v5.13.0 on 2026-05-30. Reason: skip-1 minor convention adopted 2026-05-30 — v5.11.0 pre-empted by new viz config.yaml plan; odd-only minors reserved for sequential features, even slots reserved for hotfix patches.

**Status:** drafted 2026-05-29 late evening. Renumber history: originally v5.10.7 → temporarily v5.10.8 during freshness insertion attempt → back to v5.10.7 after freshness deferred → v5.10.8 after v5.10.7 slot taken by viz fixes in the 2026-05-30 renumber → v5.11.0 in first skip-1 pass → **final: v5.13.0** after v5.11.0 pre-empted by viz config.yaml. Plan-first per I27.

**Master at draft time:** core v5.10.3 shipped + tagged.

**Sequencing:** v5.13.0. Slots after v5.11.0 (viz config.yaml) and v5.12.x hotfixes. Independent of backend-v5.4.x soak decision.

## Problem

v5.10.2 secret-gate (`gate_or_reject()`) catches real secrets but trips on legitimate content. Observed false-positives during v5.10.2 ship:

- Test fixtures with fake `ghp_` tokens (commits `cdafe04`, `36047f3`) — had to shorten fake tokens to bypass gate
- Plan documents discussing secret patterns get gated when memorized
- CHANGELOG / MIGRATION_NOTES referencing pattern strings (`ghp_{20,}`) tripped
- Backfill scan output reports include pattern fragments

Current gate is purely pattern-match. No context discrimination between "secret in user content" vs "secret discussion in docs/test/plan".

## Goals

1. **Context-aware gating** — discriminate write call-sites (memorize from user prompt vs memorize from doc-ingest pipeline vs memorize from test fixture).
2. **Allowlist** — user-managed YAML of (tag, pattern) pairs that bypass gate with audit-trail entry. Default deny.
3. **Allowlist scope** — per-tag, per-pattern, per-directory. Combinable.
4. **Audit trail** — every allowlist hit logged to `~/.yadgar/secret-gate-audit/<date>.jsonl` for review.
5. **Backward-compatible** — gate stays default-deny. Allowlist additive only.

## Non-goals

- Removing pattern strictness (v5.10.2 tightened thresholds — keep).
- ML-based secret detection (out of scope; keep regex).
- Touching kill-switch (`YADGAR_SECRET_GATE_DISABLED`) — orthogonal.

## Approach (skeleton — flesh out before dispatch)

- New module `yadgar/security/allowlist.py`: load YAML, expose `is_allowlisted(content, tags, source) -> (bool, AllowlistEntry | None)`.
- `gate_or_reject()` calls `is_allowlisted` BEFORE pattern scan. Hit → audit-log + return clean.
- Allowlist config path: `~/.yadgar/secret-gate-allowlist.yaml` (user-managed). CI test fixtures: `tests/fixtures/secret-gate-allowlist.yaml`.
- New env knob: `YADGAR_SECRET_GATE_ALLOWLIST_PATH` (default `~/.yadgar/secret-gate-allowlist.yaml`).
- Source-of-call detection: inspect `inspect.stack()` for call-site (memorize vs wiki_add vs doc-ingest). Tag `source=<callsite>` on gate decisions.

## Tests (red-first per TDD rule)

- `test_allowlist_per_tag_bypass` — tag `test-fixture` allowlist bypasses `ghp_` pattern.
- `test_allowlist_audit_log_written` — hit produces JSONL entry with all required fields.
- `test_allowlist_default_deny` — no allowlist file → behaves identically to v5.10.6.
- `test_allowlist_yaml_invalid_fails_loud` — malformed YAML → ValueError + LOUD log, no silent skip.
- `test_source_call_site_detection` — memorize from test vs prod has different `source=` tag.

## Acceptance

- All v5.10.2 false-positives in commit history can be expressed as allowlist entries.
- `scripts/check_secret_gate.py` (I26) still passes.
- New invariant: `scripts/check_allowlist_audit.py` — every allowlist hit must have audit entry.
- CHANGELOG + MIGRATION_NOTES updated.

## Open questions (resolve before dispatch)

- Allowlist YAML schema versioning?
- Audit log rotation policy (size-based vs date-based)?
- Should allowlist support pattern OVERRIDES (e.g. raise `ghp_{20,}` threshold for one tag) vs only full-bypass?
- Interaction with v5.10.5 SessionEnd capture — capture script needs to memorize findings that may include pattern strings.

## Dependencies

- v5.10.4 / v5.10.5 / v5.10.6 shipped (sequence order).
- No backend changes.
- No DB schema changes.
