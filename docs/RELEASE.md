# Release Runbook

Generic release runbook for yadgar core. Replace `<version>` with the actual semver (e.g. `5.49.4`).

## 1. Prerequisites

- PyPI API token via `op item get 'PyPI' --fields label=yadgar-api-token --reveal` or `~/.pypirc`
- Container registry: workflow rule [491179] says skip dockerhub push — local-only suffices for nix-managed deploys. Push only if cross-host pull needed.
- Codeberg push access (`git push origin master`)
- Nix repo write access (`~/git/nix/`)

## 2. Bump version

Edit `pyproject.toml:version` and `server.json:version` (and `backend_version` in `server.json` if backend rebuild). Pre-commit `check-versions` hook validates sync across `pyproject.toml`, `server.json`, `docker-compose.yml`, `flake.nix`, `uv.lock`. Commit on a feat branch.

Decide `backend_version`: bump iff `embed_service.py` / `Dockerfile.backend` changed in this release.

## 3. Build container amd64 locally

```bash
podman build --arch amd64 -t docker.io/openfantasy/yadgar:<version> -f Dockerfile .
```

Per workflow rule [490141]: ALWAYS use the full registry-prefixed tag (`docker.io/openfantasy/...`). NOT `openfantasy/...` alone — that tags as `localhost/...` and breaks systemd `ExecStart` refs.

## 4. Build PyPI artifacts + check

```bash
python3.14 -m venv /tmp/yadgar-pypi-venv
/tmp/yadgar-pypi-venv/bin/pip install --quiet build twine
/tmp/yadgar-pypi-venv/bin/python -m build --sdist --wheel --outdir /tmp/yadgar-dist
/tmp/yadgar-pypi-venv/bin/twine check /tmp/yadgar-dist/*
```

Both files should PASS.

## 5. PyPI upload

```bash
export TWINE_USERNAME=__token__
export TWINE_PASSWORD="$(op item get 'PyPI' --fields label=yadgar-api-token --reveal)"
/tmp/yadgar-pypi-venv/bin/twine upload \
  /tmp/yadgar-dist/yadgar-<version>-py3-none-any.whl \
  /tmp/yadgar-dist/yadgar-<version>.tar.gz
```

Wait ~60s for PyPI propagation. Verify:

```bash
curl -s https://pypi.org/pypi/yadgar/json | jq -r .info.version
```

## 6. Git tag + push

```bash
git tag -a v<version> -m "v<version>: <one-line summary>"
git push origin v<version>
```

## 7. Bump nix

In `~/git/nix/modules/home/yadgar.nix` bump `yadger_core_version` (and `yadger_backend_version` if backend changed). Commit + push. Run `nix-update` (alias from `~/git/nix/modules/home/shell.nix:107` — `git add` + `nix flake update` + `nixos-rebuild switch`). Local daemon hot-restarts.

## 8. Verify

```bash
yadgar daemon status       # expect Version: <version>, db ok, embed ok
yadgar update --check      # PyPI probe — should report "Up to date"
```

## 9. Optional Rocky VM smoke

```bash
podman save -o /tmp/yadgar-<version>.tar docker.io/openfantasy/yadgar:<version>
sshpass -p '<vm-root-pass>' scp /tmp/yadgar-<version>.tar root@<vm-ip>:/tmp/
sshpass -p '<vm-root-pass>' ssh root@<vm-ip> '
  podman load -i /tmp/yadgar-<version>.tar
  pipx install --force --python /usr/bin/python3.14 yadgar==<version>
  rm -f /root/.config/yadgar/secrets.env
  yadgar setup
  set -a && . /root/.config/yadgar/secrets.env && set +a
  yadgar daemon start
  yadgar daemon status
'
```

## 10. Cleanup

```bash
rm -rf /tmp/yadgar-dist /tmp/yadgar-pypi-venv /tmp/yadgar-<version>.tar
```

---

## Forgot the bump? (post-PR-open recovery)

If CI fails the release-readiness check after the PR is already open, do NOT split into a separate chore PR. Push the bump commits on the same `feat/vX.Y` branch — the PR picks them up.

```bash
git checkout feat/vX.Y
# edit pyproject.toml + docker-compose.yml
git add pyproject.toml docker-compose.yml
git commit -m "chore: bump core version A.B.C → X.Y.0"
# pre-commit auto-syncs server.json + uv.lock; re-stage + commit if it does
git add server.json uv.lock
git commit --amend --no-edit
git push origin feat/vX.Y
```

## When using the long-lived feature-branch workflow

See `claude-workflow.md`. The final integration PR (`feat/vX.Y → master`) ships the version bump in the same PR. The `no-release` label exists only for direct-to-master PRs that touch `yadgar/**` but legitimately do not ship a release (doc-only, test-only fixes that don't warrant a version bump).
