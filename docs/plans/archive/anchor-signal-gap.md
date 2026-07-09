# PLAN — anchor-signal gap: project_brief over-signals audit_anchors (#20)

**STATUS: SHIPPED 6e1629cb (#124) except shared-helper extraction; residual predicate drift fixed in fix/anchor-signal-predicate-parity (core 5.121.0).**

See docs/plans/archive/ for archived copy.

Created 2026-06-25 (improvement-train #29, group C). Root-caused against current code,
read-only. theme: signals / anchors / project_brief. priority: medium (annoyance +
trust erosion — a recommendation that resolves to zero work trains the instance to
ignore the signal).

## Problem (root-caused 2026-06-25)

`project_brief(mode="signals")` recommends `audit_anchors` whenever the project anchor
count exceeds a threshold, **independent of whether audit_anchors has anything to do**.
On a project with many healthy, unique, non-expired anchors, the signal fires every
checkpoint and `audit_anchors(dry_run=True)` returns an empty `actions` list → the
instance is told to act, acts, finds nothing, repeats. Over-signal.

A second, subtler issue: project_brief emits **action names that are not tools**.

### Code evidence

1. **The recommendation (count-only gate).** `yadgar/server/tools/project.py` —
   `_build_recommended_actions` (~line 1252): emits `audit_anchors` when
   `anchor_count_project > ANCHOR_AUDIT_THRESHOLD` (strict `>`). Threshold:
   `config.py:601` `ANCHOR_AUDIT_THRESHOLD: int = 15` → 16+ fires. The count query
   (`project.py:~1084`) already excludes expired anchors
   (`... AND (valid_until IS NONE OR valid_until > $now)`). So the gate is purely
   "how many live anchors exist", with **no check that any are actionable**.

2. **audit_anchors return shape.** `yadgar/server/tools/audit.py:705-787`
   (`audit_anchors`). Returns `{scanned, actions, dry_run, applied,
   cross_project_redundancy_candidates, anchored_by_prose_only}`. The `actions` list
   is built from four action types, each independently gated:
   - `forget_expired` — expired anchors with `migration_grace=False`
     (`_build_expire_actions` audit.py:555; `_apply_forget_expired` :623 →
     `storage.delete_memory`). **Expired anchors ARE handled** (contra the issue's
     "doesn't handle expired" framing — see Correction below).
   - `merge` — anchor pairs above `ANCHOR_REDUNDANCY_COSINE` (config.py:595, 0.92).
   - `promote` — anchors ≥ `ANCHOR_PROMOTE_WORDS` (597, 500) + ≥
     `ANCHOR_PROMOTE_HEADERS` (599, 2) words/headers with a wiki-worthy tag.
   - `verify_grace_expired_anchor` — `migration_grace=True` past expiry; always
     `skipped=True` (user-gated, never auto-applied).
   With 16 healthy unique anchors (none expired-without-grace, none ≥0.92 similar,
   none promotable) → `actions == []`. Recommendation fired; zero work.

3. **The phantom tool names.** `forget_expired_anchors` is **not a function or MCP
   tool** — grep shows it only as an *action-name string* emitted by project_brief
   (`project.py:~1280`) and referenced in the stop-hook prompt prose
   (`stop-memory-checkpoint.py:80`). Same for `merge_redundant_anchors` /
   `promote_anchor_to_wiki`. The only real tool is `audit_anchors`
   (`server/tools/__init__.py:110,187`). The mapping "these names all mean: run
   audit_anchors" exists ONLY in the stop-hook prose (lines 80-86), not at the tool
   boundary or in project_brief's own output — so any caller that isn't the stop hook
   sees four recommended "tools", three of which don't exist.

### Correction to the issue framing
The #20 ticket says audit_anchors "returns 0-actionable and doesn't handle expired."
Half right: it **does** handle expired anchors (forget_expired path, verified above).
The real defects are (a) **count-based over-signal** (recommend regardless of
actionable items) and (b) **name indirection** (phantom action names). Plan fixes
both; do NOT "add expired handling" — it already exists.

## Fix approach

Pick per user preference; (1)+(2) recommended together, small.

1. **Gate the recommendation on actual actionable items, not raw count.** In
   `_build_recommended_actions`, compute a cheap actionable-count (expired-without-grace
   OR redundant-pair-exists OR promotable-exists) and emit `audit_anchors` only when
   that is > 0 — OR keep the count gate but downgrade to an FYI when actionable==0.
   Cheapest correct version: run the same predicates audit_anchors uses, but only the
   existence checks (no full scan), and suppress the recommendation when none hold.
   - Tradeoff: duplicates a little audit logic in project_brief. Alternative: have
     `audit_anchors` expose a `count_actionable(dir)` helper that both call. Prefer the
     shared helper to avoid drift.

2. **Collapse the phantom names.** project_brief should emit a single
   `audit_anchors` action (with a human reason listing what it found:
   "3 expired, 1 redundant pair"), NOT `forget_expired_anchors` /
   `merge_redundant_anchors` / `promote_anchor_to_wiki` as separate "tools". Keeps the
   tool surface honest and removes the stop-hook's need to translate. Update the
   stop-hook prose (lines 80-86) accordingly (it already collapses them — this makes
   the server side match).

3. **(optional, if user wants the signal kept loud)** leave the count gate but add an
   `actionable: <n>` field so the instance can self-suppress. Less clean than (1).

## TDD outline (failing first)
- `test_project_brief_no_audit_signal_when_zero_actionable` — seed N>15 healthy unique
  non-expired anchors; assert `project_brief(mode="signals")` does NOT recommend
  `audit_anchors` (red today: it does).
- `test_project_brief_audit_signal_when_expired_present` — seed an expired
  (grace=False) anchor among them; assert the recommendation fires + the reason names
  the expired item.
- `test_audit_anchors_actionable_count_helper` — unit-test the shared
  `count_actionable(dir)` helper (0 for healthy set, >0 with an expired/redundant
  fixture).
- `test_project_brief_emits_only_real_tool_names` — assert no recommended action name
  is one of the phantom strings (`forget_expired_anchors` etc.); only `audit_anchors`.
- Keep green: existing project_brief signals tests + `audit_anchors` tests.

## Config / contracts touched
- No new I25 knob (reuses `ANCHOR_AUDIT_THRESHOLD`, `ANCHOR_REDUNDANCY_COSINE`,
  `ANCHOR_PROMOTE_*`). If a "suppress when not actionable" toggle is wanted, that is one
  new I25 three-way field (`config.py` + `config_yaml.py` FIELD_META +
  `config_registry.py`) — flag to user; default-on suppression needs no knob.
- No BEHAVIOR_CONTRACT row (signals are advisory, not contract-locked). The stop-hook
  prompt edit (step-4 anchor-hygiene prose) is template-only — extend
  `test_stop_memory_checkpoint_module.py` if the wording changes.

## Risks
- The actionable pre-check duplicates audit logic → drift. Mitigate with the shared
  helper (fix-approach 1, shared-helper variant).
- Suppressing the signal could hide a genuinely-needed audit if the existence checks
  are cheaper/looser than the real scan. Keep the predicates identical to
  audit_anchors' own gates (same config constants).

## Related
- `yadgar/server/tools/audit.py`, `yadgar/server/tools/project.py`,
  `yadgar/hooks/stop-memory-checkpoint.py:79-86`, `config.py:587-611` (anchor knobs).
