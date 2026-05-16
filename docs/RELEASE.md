# Release Checklist

1. All v{X.Y.0} feature PRs merged to master
2. `chore: bump core version` PR:
   - `pyproject.toml` version
   - `docker-compose.yml` CORE_VERSION default
   - `server.json` (auto-synced by pre-commit)
   - `uv.lock` (auto-synced)
   - Decide backend_version: bump iff embed_service.py / Dockerfile.backend changed
3. Merge bump PR
4. `git tag v{X.Y.0} && git push origin v{X.Y.0}`
5. Update `nix/modules/home/yadgar.nix` core_version + backend_version
6. `home-manager switch`
7. Append release entry to `yadgar-roadmap-future-improvements` wiki
8. Verify deployed: `recall("yadgar version")` returns new version
