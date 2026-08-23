# C2 — Task 334: Add `.mypy_cache` to `.dockerignore` (dev-only parity)

## Goal

Stop `.mypy_cache` from being baked into the `yadgar` Docker image by adding
it to `/home/max/git/yadgar/.dockerignore`. The file currently lists
`.ruff_cache`, `.pytest_cache`, `.hypothesis` (lines 12-14) but omits
`.mypy_cache`, so a local run of `mypy` (or any mypy invocation that leaves
the cache behind) leaves ~2.4 MB of cache in the build context, inflating the
`COPY . /app` layer and changing the layer digest on every local rebuild
even when no source changed. Mirrors the existing cache-directory pattern.

## Pre-conditions

- `/home/max/git/yadgar/.dockerignore` is the file to edit. Currently 30 lines.
- The cache directory `.mypy_cache/` is a project-root relative directory
  produced by mypy invocations. No source code references it; nothing reads
  from it at runtime.
- The other cache directories already ignored
  (`.ruff_cache` line 12, `.pytest_cache` line 13, `.hypothesis` line 14)
  establish the pattern: bare directory name, no glob, no leading slash.
- Dev-only: production CI does not produce `.mypy_cache` because mypy is not
  invoked as a step in `.forgejo/workflows/ci-release.yaml`. The fix is for
  developer-local `docker build` runs only.

## Step-by-step

1. **Open `/home/max/git/yadgar/.dockerignore`**.

2. **Insert `.mypy_cache` between `.hypothesis` and `.local-review`** so the
   three cache directories stay grouped:
   - Before (lines 12-15):
     ```
     .ruff_cache
     .pytest_cache
     .hypothesis
     .local-review
     ```
   - After:
     ```
     .ruff_cache
     .pytest_cache
     .hypothesis
     .mypy_cache
     .local-review
     ```

3. **Add a one-line comment** explaining the grouping (matches the existing
   `PLAN*.md` comment style on line 23 but kept terse):
   ```
   # Dev tooling caches: ruff, pytest, hypothesis, mypy
   ```
   Place it immediately above the `.ruff_cache` line so all four cache
   entries are explicitly grouped. This is a docs-by-comment choice — the
   user can opt to skip the comment if they prefer the existing minimal style.

4. **Verify the `uv.lock` carve-out still reads correctly** (lines 24-29):
   - The NOTE block explicitly says `uv.lock is intentionally NOT ignored`
     because `Dockerfile.ci` `COPY uv.lock` (see `Dockerfile.ci:106`) bakes
     the lock for parity. Untouched by this car.

5. **Test the fix** (locally, no push):
   - `touch .mypy_cache/.gitkeep` (no-op if mypy already populated it).
   - `docker build -f Dockerfile -t yadgar:dev-test .`
   - `docker run --rm yadgar:dev-test ls /app | grep -c mypy_cache` →
     expect `0`.
   - Compare layer digests before/after:
     `docker history yadgar:dev-test --no-trunc` → the
     `COPY . /app` line's SHA should now be stable across local rebuilds
     when only `.mypy_cache` content changed.

## Verification

- `cat /home/max/git/yadgar/.dockerignore` shows `.mypy_cache` on a new line
  in the cache block.
- A `docker build` invocation with a populated `.mypy_cache/` in the repo
  root produces an image where `/app/.mypy_cache` does NOT exist.
- The `COPY . /app` layer digest is byte-identical between (a) a build run
  after `mypy` produced `.mypy_cache/` and (b) a build run after `rm -rf
  .mypy_cache` — proves the cache was excluded, not merely tolerated.
- The existing `tests/test_dockerignore_ci_copy_consistency` test referenced
  on `.dockerignore:29` should still pass; the test guards
  `Dockerfile.ci`'s `COPY pyproject.toml` + `COPY uv.lock` path which this
  car does not touch.

## Risks / rollback

- **Behavior change is zero-risk**: no runtime path reads `.mypy_cache` from
  the image. The image never shipped it intentionally; removing it from
  the build context is a deletion-only change.
- **`Dockerfile` vs `Dockerfile.ci` paths**: the fix lives in `.dockerignore`
  which Docker applies to BOTH `Dockerfile` (line 1 `FROM python:3.14-slim`)
  AND `Dockerfile.backend` (line 1 `FROM python:3.14-slim`). Both COPY the
  project root; both now skip `.mypy_cache`. Verified by reading the
  Dockerfile headers.
- **CI cache directories**: the existing `.ruff_cache`, `.pytest_cache`,
  `.hypothesis` entries already handle CI's tooling caches. `.mypy_cache`
  is dev-only because mypy is not a CI step in
  `.forgejo/workflows/ci-release.yaml` or `ci-pr.yaml`. No CI coverage
  gap is created by omitting it from CI.
- **Rollback**: revert the one-line addition. Trivially safe.

## Approx LOC + risk class

- LOC: +1 (the new `.mypy_cache` line), optionally +1 comment.
- Risk class: **trivial** (build-context exclusion, no runtime impact).
- Time cost: <5 min for the edit + a single `docker build` sanity check.

## Source evidence

- `/home/max/git/yadgar/.dockerignore:12-14` — the three existing cache
  directories that establish the pattern this car extends.
- `/home/max/git/yadgar/.dockerignore:23` — `PLAN*.md` comment demonstrates
  the inline comment style permitted in this file.
- `/home/max/git/yadgar/.dockerignore:24-29` — `uv.lock` carve-out NOTE.
  Confirms `uv.lock` is intentionally NOT ignored and is referenced by
  `Dockerfile.ci` `COPY uv.lock`. The `test_dockerignore_ci_copy_consistency`
  test mentioned here is the regression guard.
- `/home/max/git/yadgar/Dockerfile:1` — root Dockerfile (also affected by
  `.dockerignore`).
- `/home/max/git/yadgar/Dockerfile.backend:1` — backend Dockerfile (also
  affected).
- `/home/max/git/yadgar/Dockerfile.ci:104-106` — `Dockerfile.ci` copies only
  `pyproject.toml` + `uv.lock`, not the full project tree. Its build context
  is not sensitive to `.mypy_cache` because it never does `COPY . /app`.
  The fix matters for `Dockerfile` and `Dockerfile.backend` only.
- `/home/max/git/yadgar/.forgejo/workflows/ci-release.yaml` — not read in
  this car; not affected because CI does not run mypy.
