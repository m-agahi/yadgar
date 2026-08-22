# C1 — task 247: adr_get emits empty-string rationale/alternatives/revisit_trigger

## Goal

Make `adr_get` stop emitting `rationale`, `alternatives`, `revisit_trigger` as the empty
string `""`. The ADR table has no such columns — the prose lives in the body page as
flat bullet items (`- rationale: ...`, `- alternatives: ...`, `- revisit_trigger: ...`),
rendered by `ADR.to_markdown_body()` in `yadgar/_shared/contracts/models.py:290–305` and
assembled by `_build_adr_body` in `yadgar/core/server/tools/adr_render.py:47–86`. The
current return shape hands the caller three useless `""` fields that they read as "this
ADR has no rationale/alternatives/revisit_trigger". Omit the keys entirely on
`adr_get`'s output — a key-absent return is the unambiguous signal that the caller
should read the body page.

## Root cause

`adr_get`'s response is built by `_build_adr_get_response(body, row_result)` at
`yadgar/core/server/tools/adr.py:786–802`, which merges the body dict with the row
metadata produced by `_row_to_response_metadata(row)` at `yadgar/core/server/tools/adr.py:805–826`:

```python
return {
    "date": row.get("decided_on") or "",
    "rationale": "",  # prose lives on the body page (D4)
    "alternatives": "",  # ditto
    "revisit_trigger": "",  # ditto
    ...
}
```

Lines 811/812/813 each hard-code `""`. Comments explicitly acknowledge the prose lives
elsewhere, yet the keys are still emitted — caller can read `""` as "none".

Cross-check that the columns don't exist in the schema:
- `yadgar/_shared/storage/sql/ledger_columns.py:74–78` defines `ADR_COLUMNS = (id,
  project_id, title, status, decided_on, subsystem, tier, body_slug, superseded_at,
  created_at, updated_at)` — 11 columns total, none named `rationale` /
  `alternatives` / `revisit_trigger`.
- `tests/_shared/test_adr_car_b2_ledger_sql.py:42–44, 101–103` confirms migration 002
  builds the `adr` table from exactly those 11 columns; no rationale-style columns
  exist anywhere.

Cross-check that nothing depends on the empty strings being present:
- `yadgar/tests/core/test_adr_tools_car_f.py:435` has a vacuous assertion
  (`result.get("rationale") is not None or result.get("supersedes") is not None or True`)
  that still passes with the keys absent — no test asserts the value of these three
  fields.
- No test asserts `adr_get["rationale"] == ""`. Removing the keys is safe.

The fix is local to one function in one file.

## Step-by-step

1. **Edit `_row_to_response_metadata` in `yadgar/core/server/tools/adr.py:805–826`.**
   Drop the three `""` entries from the returned dict. The new shape:

   ```python
   return {
       "date": row.get("decided_on") or "",
       # rationale / alternatives / revisit_trigger live on the body page (D4).
       # D5 merge is additive-only: emit only keys the row actually populates.
       "supersedes": _fmt_supersedes(row.get("supersedes")),
       "superseded_by": _fmt_superseded_by(row.get("superseded_by")),
       "subsystem": row.get("subsystem") or "",
       "tier": row.get("tier") or "",
       "baseline_hash": row.get("baseline_hash") or "",
       "content_hash": row.get("content_hash") or "",
   }
   ```

   Keep the `D5 additive-only` rationale (the comment moves to a single line above
   `supersedes`) — `_build_adr_get_response` at L786 already documents D5's contract.

2. **Sanity-check the rest of `adr_get`'s return shape.** Confirm the function still
   emits `date`, `supersedes`, `superseded_by`, `subsystem`, `tier`, `baseline_hash`,
   `content_hash`, the body content fields, and the merged tags. `_build_adr_get_response`
   at L786–802 already merges body + row metadata — the only change is that the row side
   contributes seven keys instead of ten.

3. **Regression test.** New file
   `yadgar/tests/core/test_c1_task_247_adr_get_omits_empty_keys.py`. Two tests:

   - `test_adr_get_omits_empty_prose_keys` — insert an ADR via `adr_add` (real backend
     SQLite + tmp_path fixture), call `adr_get`, assert `"rationale"` not in result,
     `"alternatives"` not in result, `"revisit_trigger"` not in result. Pre-fix: red
     (all three present, value `""`). Post-fix: green.
   - `test_adr_get_still_emits_real_metadata` — same setup; assert `date`, `baseline_hash`,
     `content_hash`, `supersedes`, `superseded_by`, `subsystem`, `tier` ARE present.
     Both pre-fix and post-fix green — guards the merge path didn't break.

   Pattern: follow `yadgar/tests/core/test_adr_tools_car_f.py:381–438`
   (`TestAdrGetMerge.test_adr_get_post_migration_merges_row_metadata`) for fixture
   style. Use `tmp_path` storage + `embeddings` fixture from that file. The vacuous
   `or True` at L435 can stay — it's not load-bearing.

4. **Run targeted tests.** `pytest
   yadgar/tests/core/test_c1_task_247_adr_get_omits_empty_keys.py -v`. Then
   `pytest yadgar/tests/core/ -k adr -q` (regression sweep across all ADR tests).

5. **Update wiki page if any doc lists the return shape.** `git grep -l "rationale"
   yadgar/docs/ 2>/dev/null` and `yadgar/core/server/tools/adr*.py` — if any doc claims
   `adr_get` returns `rationale`, update it. Do NOT change `adr_add` — `adr_add` writes
   to the body page (`yadgar/core/server/tools/adr.py:480–508` →
   `_build_adr_body` in `adr_render.py:80–86`) and is unaffected.

## Verification

Red → green is the `test_adr_get_omits_empty_prose_keys` assertion. Pre-fix:
`assert "rationale" not in result` fails (key present with value `""`). Post-fix:
green (key absent).

Acceptance:

- `adr_get("m-agahi/yadgar", "ADR-0227")` returns a dict whose top-level keys do NOT
  include `rationale`, `alternatives`, or `revisit_trigger`.
- The dict still contains `date`, `supersedes`, `superseded_by`, `subsystem`, `tier`,
  `baseline_hash`, `content_hash` — unchanged.
- A caller reading the body page (via `body["content"]` or equivalent) sees the prose
  under `- rationale:` / `- alternatives:` / `- revisit_trigger:` bullets, as before.

## Risks / rollback

- **Risk:** A downstream caller relies on the keys being present (even with `""`
  values). Search: `git grep -nE 'adr_get|\.get\("rationale"\)|\.get\("alternatives"\)|\.get\("revisit_trigger"\)' -- '*.py'`.
  The Explore agent found no such caller (only one vacuous `or True` test). Mitigation:
  if a real caller exists, its reading is currently broken anyway (`""` misread as
  "none"), and key-absent surfaces the failure more loudly.
- **Risk:** Removing the keys breaks D5's "additive-only" contract documentation.
  D5 (ADR-0209 §14.3) governs what `adr_get` MAY add going forward; it does NOT
  require it to keep keys that were never populated. The fix removes three
  never-populated keys — net D5 surface shrinks, contract preserved.
- **Risk:** `_row_to_response_metadata` is also called from other code paths.
  Search: `git grep -n "_row_to_response_metadata" -- '*.py'`. If it's called
  outside `_build_adr_get_response`, the same key-absent semantics should apply.
  Mitigation: if a second caller exists and depends on the empty strings, file a
  follow-up rather than expand scope — task 247 is one-call-site fix.
- **Rollback:** revert the one-file diff. The regression test is in its own file.
  No migration, no schema change, no caller-facing wire-format version bump
  (the keys are gone, not renamed — backward-compatible break because callers
  reading `""` as truth were already wrong).

## Approx LOC + risk class

- Source diff: ~6 lines removed in `yadgar/core/server/tools/adr.py:805–826`
  (three dict entries plus their comments collapse to one comment).
- New test file: ~80 lines.
- Risk class: **LOW** — local to one function, no schema change, no caller
  depends on the empty values (the only assertion that names them is vacuous),
  removal is the unambiguous correct signal given the schema has no such columns.

## Source evidence

- `yadgar/core/server/tools/adr.py:786–802` — `_build_adr_get_response`; merge of body +
  row metadata, additive-only.
- `yadgar/core/server/tools/adr.py:805–826` — `_row_to_response_metadata`; the three
  `""` lines at L811/812/813 are the fix site.
- `yadgar/_shared/storage/sql/ledger_columns.py:74–78` — `ADR_COLUMNS` tuple confirms
  the table has no `rationale` / `alternatives` / `revisit_trigger` columns.
- `yadgar/core/server/tools/adr.py:480–508` — `adr_add` writes rationale/alternatives/
  revisit_trigger into the body page via `_build_adr_body`, not into a DB column.
- `yadgar/core/server/tools/adr_render.py:47–86` — `_build_adr_body` body assembly;
  confirms prose lives in `content` as flat bullets (`- rationale: ...`).
- `yadgar/_shared/contracts/models.py:290–305` — `ADR.to_markdown_body()` renders the
  prose as bullets, not `## Rationale:` / `## Alternatives:` / `## Revisit trigger:`
  sub-sections — schema-less body, no reliable parse.
- `yadgar/tests/_shared/test_adr_car_b2_ledger_sql.py:42–44, 101–103` — migration 002
  fixture; 11 columns, none of the three names.
- `yadgar/tests/core/test_adr_tools_car_f.py:381–438` — `TestAdrGetMerge`; the only
  test naming the three keys (vacuous `or True` at L435). Reference for fixture style.
- `yadgar/tests/core/test_car5_project_id_create_enforcement.py:81–182,
  188–234` — registry check reference, not directly applicable here but shows
  assertion style for ADR tools tests.
