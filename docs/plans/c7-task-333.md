# C7 — task 333: `yadgar stats --project` scopes against the wrong column

## State going in

Task 333 in the ledger: `stats.py --project UNDER-COUNTS TODAY: resolves
args.project to a filesystem PATH, compares against directory_context which new
writes stamp with an IDENTITY. Misses every post-re-key row silently`.

The bug surfaces as a single integer on a freshly-restamped corpus: every
per-project count is small or zero, but the database clearly holds N memories
for the project. The user pastes the output, reads "0 memories", assumes the
project is empty.

## What the bug actually is

`yadgar/core/cli/stats.py:781` does:

```python
project = str(Path(args.project).resolve()) if args.project else None
```

That is the bug's first half — `args.project` is whatever they typed on the
command line. The CLI contract is `--project <id-or-path>`. For a path-shaped
value, `Path.resolve()` is correct. For an identity-shaped value
(`m-agahi/yadgar`), `Path("m-agahi/yadgar").resolve()` returns
`/home/max/git/m-agahi/yadgar` (or a similar absolute path that has never been
written). Then the 12 SELECTs on `directory_context = $p` all compare against
that absolute-path string.

The new write path (Car C10f/C10g, PR #62 onward) stamps `directory_context`
with the **resolved identity** (`m-agahi/yadgar`), not a filesystem path. So:

- Legacy rows: `directory_context = /home/max/git/yadgar` → count them ONLY
  if the user passed the exact resolved path.
- Post-re-key rows: `directory_context = m-agahi/yadgar` → NEVER counted by
  the path-mode SELECT.

The HTTP fallback path (`_try_http_path`) passes `args.project` raw into
`?project=<value>`. The daemon already interprets that as `project_id` after
C7/C10f, so the daemon path is correct. The bug is the host-side fallback —
which is the path that fires when the daemon is unreachable (carried-over
container installs, locked datastore).

## What the fix is

1. **Resolve identity up front** — call `resolve_cli_project(args.project,
   cwd, required=False)` like `yadgar context` already does (336's pattern).
   That returns either the resolved identity string or `None`. The current
   `Path(...).resolve()` is the wrong resolution; it treats every input as a
   path.

2. **Compare against `project_id`, not `directory_context`** — the SELECTs
   already use `$p` as the bound value; the column they compare is what
   needs to change. `project_id` is the new identity column that new writes
   stamp. Scoping on it is the same shape as C7-336: the legacy rows still
   hold paths in `directory_context`, but every post-re-key row holds its
   identity in `project_id`, and the resolved identity matches.

3. **HTTP fallback parity** — the daemon already scopes by `project_id`
   (the wire-level shape). The host path now does the same.

## Files touched

| File | Edit |
|---|---|
| `yadgar/core/cli/stats.py` | Add `resolve_cli_project` call, switch SELECTs to scope on `project_id` |
| `yadgar/tests/cli/test_stats_module.py` | Pin tests at the SQL-text level (same shape as the 336 pin tests) |

## Pins (test plan)

- `test_cmd_stats_project_resolves_identity_not_filesystem_path` — `args.project="m-agahi/yadgar"` is passed to `_query_core_counts` as the resolved identity string, NOT `Path("m-agahi/yadgar").resolve()`.
- `test_cmd_stats_selects_on_project_id_column_not_directory_context` — the SQL text uses `project_id = $p`, never `directory_context = $p` (analogous to `test_hot_query_selects_on_project_id_column` in test_cli_context_module.py:204).
- `test_cmd_stats_unresolvable_project_binds_none` — `args.project=/nonexistent` falls through `resolve_cli_project` to `None` and binds `None`, matching no rows (rather than binding the raw filesystem path).
- `test_cmd_stats_path_input_still_works` — `args.project=/home/max/git/yadgar` (raw path) resolves to `m-agahi/yadgar` (the identity) and counts the project's rows.

## Acceptance

- All targeted tests green on `train/c1-c10-bug-bag`.
- Task 333 closed in BOTH harness TaskList AND yadgar ledger (with `body_slug` stamped and `completed_at` stamped; verified via `task_get` per memory `yadgar-ok-true-is-not-evidence`).
- I33 / I30 / complexity / I13 gates clean.

## What this car is NOT

- Not a restamp of legacy rows (task 41 is SUPERSEDED; ADR-0233 keeps the legacy paths for the project's own internal use).
- Not a fix to the HTTP daemon endpoint (already correct post-C7).
- Not a column rename — `directory_context` stays; `project_id` is the new read key.

## Follow-ups (out of scope for this car)

- Task 308: SR prediction bucket reads `directory_context` against `project_id`. Same class of fix, different code path, separate car.
- Task 50: cross-project wiki scoping drift — 9 legacy rows hold the wrong `project_id`. DATA half, write-side half-heal shipped earlier.
