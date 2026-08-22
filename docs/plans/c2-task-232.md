# C2 — Task 232: Manually rebuild + push yadgar-ci / yadgar-ci-viz images

## Goal

Ship license-clean `yadgar-ci` and `yadgar-ci-viz` images to `docker.io/openfantasy/`.
The release gate (`.forgejo/workflows/ci-release.yaml`) covers `yadgar` and `yadgar-backend`
only — the two CI-runner images are pulled by every CI job but stay
license-non-compliant until an operator rebuilds and pushes them by hand.
Source-of-record for what ships in the image: `Dockerfile.ci` (`/home/max/git/yadgar/Dockerfile.ci`),
`Dockerfile.ci-viz` (`/home/max/git/yadgar/Dockerfile.ci-viz`), and the `ci-pr.yaml`
pin (ADR-0135).

## Pre-conditions

- Read ADR-0188 ("Container images are CI-built and registry-fetched — never
  build yadgar images locally"). The clause covers `yadgar` + `yadgar-backend`
  images. Task 232 is the deliberate **exception**: the CI runner images have no
  CI build path, so a manual rebuild is the only sanctioned route.
- `pyproject.toml` version is the authority for the version LABEL / tag
  (`Dockerfile.ci:21`, `ARG YADGAR_VERSION=5.136.0`).
- `Dockerfile.ci-viz:12` is build-arg wired (`ARG BASE_TAG=5.136.0`) — bump in
  lockstep with the `yadgar-ci` push.
- `.forgejo/workflows/ci-pr.yaml` must be edited to point
  `container.image: docker.io/openfantasy/yadgar-ci:${{ vars.YADGAR_VERSION }}`
  to the new tag (ADR-0135) BEFORE pushing the image; otherwise `docker pull`
  on the runner hits a stale tag.
- Operator has `docker` (or `podman`) on a linux/amd64 host with push creds to
  the `openfantasy` Docker Hub namespace.

## Step-by-step

1. **Determine the version** (file:line anchored):
   - `pyproject.toml` → `[project] version = "..."` is the source.
   - If the LABEL on `Dockerfile.ci:22` (`org.opencontainers.image.version`)
     or the default `ARG YADGAR_VERSION` on `Dockerfile.ci:21` are stale
     relative to `pyproject.toml`, fix them BEFORE building
     (the default documents "version current at the last LABEL touch" per
     `Dockerfile.ci:19-20`).
   - Same for `Dockerfile.ci-viz:17` (`LABEL org.opencontainers.image.version`)
     and `Dockerfile.ci-viz:12` (`ARG BASE_TAG`).

2. **Build `yadgar-ci`** (from repo root):
   - Command:
     ```
     docker buildx build \
       --platform linux/amd64 \
       --build-arg YADGAR_VERSION=$(python3 -c 'import tomllib;print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])') \
       -f Dockerfile.ci \
       -t docker.io/openfantasy/yadgar-ci:<VERSION> \
       -t docker.io/openfantasy/yadgar-ci:latest \
       --load .
     ```
   - Layer-ordering is load-bearing (see `Dockerfile.ci:151-159`): the ruff
     bake is LAST so heavy HF model layers above stay cacheable. Do not
     reorder.

3. **Build `yadgar-ci-viz`** (after the `yadgar-ci` push so the `FROM`
   resolves):
   - Command:
     ```
     docker buildx build \
       --platform linux/amd64 \
       --build-arg BASE_TAG=<VERSION> \
       -f Dockerfile.ci-viz \
       -t docker.io/openfantasy/yadgar-ci-viz:<VERSION> \
       -t docker.io/openfantasy/yadgar-ci-viz:latest \
       --load .
     ```
   - `Dockerfile.ci-viz:13` does `FROM docker.io/openfantasy/yadgar-ci:${BASE_TAG}`
     — tag must exist on the registry before this resolves.

4. **Push both images** (in this order):
   - `docker push docker.io/openfantasy/yadgar-ci:<VERSION>`
   - `docker push docker.io/openfantasy/yadgar-ci:latest`
   - `docker push docker.io/openfantasy/yadgar-ci-viz:<VERSION>`
   - `docker push docker.io/openfantasy/yadgar-ci-viz:latest`

5. **Edit `ci-pr.yaml` to track the new tag** (ADR-0135):
   - Update `vars.YADGAR_VERSION` in the Forgejo repo UI
     (`Settings → Secrets → Actions → Variables`). The workflow body itself
     already reads `${{ vars.YADGAR_VERSION }}` per ADR-0135 — no source edit
     needed if the workflow is already on the var-driven form.
   - If any of the workflows (`ci-pr.yaml`, `ci-release.yaml`, `eval.yaml`,
     `perf.yaml`) still hardcode a tag, roll forward to the var-driven form
     per ADR-0135 first. `Dockerfile.ci-viz`'s `FROM` cannot read Actions vars
     — it stays build-arg wired.

6. **Smoke-pull from a fresh runner** (sanity):
   - `docker pull docker.io/openfantasy/yadgar-ci:<VERSION>`
   - `docker run --rm docker.io/openfantasy/yadgar-ci:<VERSION> python --version`
     expect `Python 3.14.x`.
   - `docker run --rm docker.io/openfantasy/yadgar-ci:<VERSION> gitleaks version`
     expect `8.30.1`.
   - `docker run --rm docker.io/openfantasy/yadgar-ci:<VERSION> surreal version`
     expect `3.1.5`.
   - `docker run --rm docker.io/openfantasy/yadgar-ci:<VERSION> ruff --version`
     expect `0.15.10`.

7. **Trigger a PR-side run** that exercises the new tag:
   - Open a throwaway PR (or push an empty commit) and confirm `ci-pr.yaml`'s
     `container.image` resolves and tests run green on the new image.

## Verification

- `docker manifest inspect docker.io/openfantasy/yadgar-ci:<VERSION>` shows
  `linux/amd64` and a non-zero layer count (proves push completed).
- `ci-pr.yaml`'s next green run references the new `<VERSION>` in the run logs
  (`docker.io/openfantasy/yadgar-ci:<VERSION>` line in the "Pulling container
  image" step).
- License compliance: `python3 scripts/check_third_party_licenses.py` runs
  green. License files were baked at `Dockerfile.ci:170-171`
  (`COPY LICENSE NOTICE THIRD-PARTY-LICENSES /usr/share/doc/yadgar/` and
  `COPY third-party /usr/share/doc/yadgar/third-party`).
- License artifacts present inside the image:
  - `docker run --rm docker.io/openfantasy/yadgar-ci:<VERSION> ls /usr/share/doc/yadgar`
    shows `LICENSE  NOTICE  THIRD-PARTY-LICENSES  third-party/`.

## Risks / rollback

- **License non-compliance window**: every minute the old non-compliant tag
  is pulled, jobs ship code. Mitigate by pausing CI (or blocking merges) until
  the new image is live; ADR-0188 nails the cost ("podman's d...").
- **Bake failure on an unpinned dep**: gitleaks v8.30.1, SurrealDB v3.1.5,
  ruff 0.15.10 all have pinned SHAs / versions (`Dockerfile.ci:81-101`,
  `Dockerfile.ci:157-159`). A registry move or removed release tarball fails
  the build loud, not silent.
- **Layer-cache invalidation**: bumping any earlier layer (e.g. apt deps on
  `Dockerfile.ci:62-74`) re-bakes the 1GB+ HF model layers on `Dockerfile.ci:125-149`.
  Prefer bumping ARGs (`YADGAR_VERSION`, `GITLEAKS_VERSION`, `SURREAL_VERSION`,
  `RUFF_VERSION`) over touching the `RUN apt-get` line.
- **Rollback**: re-pin `vars.YADGAR_VERSION` to the last known-good tag.
  Both the old and new image stay on Docker Hub tagged, so rollback is a one-
  field UI change.
- **`yadgar-ci-viz` base lag**: if the yadgar-ci push fails or is skipped,
  `Dockerfile.ci-viz:13`'s `FROM` resolves to a stale tag and the rebuild
  silently bakes against the wrong base. The smoke-pull step (Step 6) is
  the only check that catches this; do not skip it.

## Approx LOC + risk class

- LOC: 0 source lines. Operations are build/push/UI edit.
- Risk class: **medium** (operational + license). The blast radius is every
  CI run, but the path is fully reversible by repinning the var.
- Time cost: 15-30 min per image for the build + 1-3 min push. Plan for one
  rebuild iteration.

## Source evidence

- `/home/max/git/yadgar/Dockerfile.ci:21-22` — `ARG YADGAR_VERSION` default +
  LABEL wiring.
- `/home/max/git/yadgar/Dockerfile.ci:62-74` — apt system deps layer.
- `/home/max/git/yadgar/Dockerfile.ci:81-89` — gitleaks bake + SHA256.
- `/home/max/git/yadgar/Dockerfile.ci:93-101` — SurrealDB bake + SHA256.
- `/home/max/git/yadgar/Dockerfile.ci:115-120` — uv lock-parity install.
- `/home/max/git/yadgar/Dockerfile.ci:125-149` — HF / CrossEncoder / seq2seq
  model bakes (the 1GB+ cacheable block).
- `/home/max/git/yadgar/Dockerfile.ci:151-159` — ruff bake LAST to preserve
  cache.
- `/home/max/git/yadgar/Dockerfile.ci:170-171` — LICENSE/NOTICE/THIRD-PARTY
  COPYs (the license-cleanup root cause for Task 232).
- `/home/max/git/yadgar/Dockerfile.ci-viz:12-13` — `ARG BASE_TAG` + `FROM`.
- `/home/max/git/yadgar/Dockerfile.ci-viz:17` — image version LABEL.
- `/home/max/git/yadgar/.forgejo/workflows/validate.yaml:17` —
  `container.image: python:3.14-slim` (NOTE: this workflow does NOT use the
  custom CI image — Task 232 is for the OTHER workflows, namely `ci-pr.yaml`).
- ADR-0135 — `vars.YADGAR_VERSION` var-driven image tag pattern (Forgejo
  expands `${{ vars.* }}` in `jobs.<id>.container.image`).
- ADR-0188 — `yadgar` + `yadgar-backend` must never be built locally. The
  CI-runner images (`yadgar-ci`, `yadgar-ci-viz`) are NOT covered by this ADR
  and remain the only legitimate manual-build path.
