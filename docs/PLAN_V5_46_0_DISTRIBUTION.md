# PLAN — v5.46.0: Distribution (PyPI Polish + Homebrew + Nix Flake + Codeberg Release Automation + SBOM)

**Status:** drafted 2026-05-31. Plan-first per I27.

**Audit lineage:** prior Explore agent (post-v5.25.0 setup audit) flagged GAPS: no Homebrew tap, no Nix flake (only home-manager module in separate repo), no Codeberg release-asset automation, no SBOM tooling. PyPI metadata is functional but ships partial classifier/keyword set.

**Ships in train:** v5.45.0 → v5.46.0 → v5.47.0 (foundation → distribution → updates).

**Depends on:** v5.45.0 shipped — install asset layout + multi-OS daemon hooks.

**Effort estimate:** 3–4 calendar days.

**Downstream:**
- v5.47.0 needs detected install-method (pipx / brew / nix-flake / container). v5.46.0 makes brew + nix install paths reality.

See also `docs/DECISIONS.md` — 2026-05-31 PD-37 (setup mechanism; v5.46 implements the distribution side).

---

## Goal — three first-class install paths + reproducible release artifacts

Ship four parallel distribution channels:

1. **PyPI** (already live, polish only) — `pipx install yadgar`. Add missing classifiers, keywords, extras documentation.
2. **Homebrew** — `brew install maxagahi/yadgar/yadgar`. New tap repo `homebrew-yadgar` on Codeberg. Formula references the PyPI wheel + container image.
3. **Nix flake** — `nix run codeberg:maxagahi/yadgar#yadgar` OR `nix profile install ...`. New `flake.nix` at yadgar repo root. Outputs: `packages` / `apps` / `nixosModules` / `homeManagerModules`.
4. **Codeberg release automation** — Forgejo Actions on tag push: source tarball + container manifest + checksums + SBOM (CycloneDX JSON) + brew formula bump PR + nix flake input bump PR.

Container image source-of-truth stays at `docker.io/openfantasy/yadgar` (existing). Release manifest in Codeberg points to that image; no duplicate hosting.

---

## Non-goals (explicit)

- **No new install scripts.** All install-side UX delivered in v5.45.0.
- **No update mechanism.** That's v5.47.0.
- **No Windows / WSL2-specific path.** Compose-via-pipx works on WSL2 today; native Windows installer is a v6+ concern.
- **No PyPI publishing infrastructure rewrite.** Existing Forgejo OIDC trusted-publisher works; v5.46 only polishes metadata.
- **No alternate registries (quay.io, ghcr.io).** Docker Hub mirror only.
- **No SPDX SBOM in v5.46.** CycloneDX JSON is the v5.46 default; SPDX is a future variant.
- **No signed release artifacts (sigstore / cosign).** v5.47+ candidate; documented as deferred.

---

## Current state (verified from code, 2026-05-31)

| Component | Status | Gap |
|---|---|---|
| PyPI publishing | READY — Forgejo OIDC trusted-publisher works (v5.25.1 publish confirmed) | Metadata polish: keywords, classifiers, optional-dependency descriptions |
| `pyproject.toml` classifiers | Has 7 classifiers (Development Status, License, Programming Language, Topic) | Missing: `Topic :: System :: Filesystems`, `Operating System :: POSIX :: Linux`, `Operating System :: MacOS`, `Environment :: Console` |
| `pyproject.toml` license metadata | `license = "Apache-2.0"` (string) + classifier `"License :: OSI Approved :: MIT License"` MISMATCH | Fix: confirm canonical license (Apache-2.0 per file), remove MIT classifier, add `License :: OSI Approved :: Apache Software License`. **Cross-check:** read `LICENSE` file in repo before fix. |
| Homebrew tap | DOES NOT EXIST | Create `homebrew-yadgar` on Codeberg. New repo, separate. |
| Nix flake | DOES NOT EXIST in yadgar repo. Only `home-manager` module at `/home/max/git/nix/modules/home/yadgar.nix` (separate repo) | Create `flake.nix` at yadgar root. |
| Codeberg release automation | NONE — no `.forgejo/workflows/release.yaml`; CI workflow has `publish` job but no release-asset generation | Add release job: source tarball + container manifest + checksums + SBOM + brew/nix bump PRs |
| SBOM tooling | NONE | Add `cyclonedx-py` to `[dev]` extras; wire into release workflow |
| Container source-of-truth | `docker.io/openfantasy/yadgar` + `docker.io/openfantasy/yadgar-backend` | Stays; release manifest references these (not duplicate hosting) |
| Version sync | Manual: `pyproject.toml` + `server.json` + nix module + (future) brew formula | New: `scripts/bump_version.py` — single-source-of-truth bumper |

---

## Scope — concrete file changes

### New repos (separate from yadgar)

| Repo | Purpose | Codeberg URL |
|---|---|---|
| `homebrew-yadgar` | Homebrew tap. Holds `Formula/yadgar.rb`. Auto-updated on tag push via Forgejo Actions PR. | `codeberg.org/maxagahi/homebrew-yadgar` |

### New files (in yadgar repo)

| Path | Purpose |
|---|---|
| `flake.nix` (repo root) | Nix flake with outputs: `packages.<system>.{yadgar, yadgar-backend}` / `apps.<system>.yadgar` / `nixosModules.yadgar` / `homeManagerModules.yadgar`. Builds the python package + wraps container image references. |
| `flake.lock` (repo root) | Nix flake lock file; pinned `nixpkgs` + `flake-utils` inputs. |
| `.forgejo/workflows/release.yaml` (or extend existing `ci.yaml`) | New release job — triggers on `tags: v*` push: build source tarball, generate SBOM, generate checksums, attach to Codeberg release, open brew + nix bump PRs. |
| `scripts/bump_version.py` | Single-source-of-truth bumper. Reads/writes version in `pyproject.toml`, `server.json`, `flake.nix` (`version` attr), `Formula/yadgar.rb` (in homebrew-yadgar repo via separate PR), `~/git/nix/modules/home/yadgar.nix` (`yadger_core_version` literal — needs nix-repo PR or manual sync). |
| `scripts/gen_sbom.sh` | Wrapper around `cyclonedx-py environment` → outputs `dist/yadgar-<version>-sbom.cdx.json`. |
| `Formula/yadgar.rb.in` (template in yadgar repo, NOT in homebrew-yadgar) | Homebrew formula template. Substitutes version, sha256, container image tag. Used by release workflow to generate the actual `Formula/yadgar.rb` in the tap repo. |
| `docs/INSTALL.md` | Per-platform install instructions: PyPI / Homebrew / Nix flake / source. Cross-references `docs/PLAN_V5_45_0_SETUP_FOUNDATION.md` for the post-install `yadgar install` step. |

### Files in homebrew-yadgar tap repo (separate)

| Path | Purpose |
|---|---|
| `Formula/yadgar.rb` | Homebrew formula. Auto-generated by yadgar release workflow PR. |
| `README.md` | Tap usage: `brew tap maxagahi/yadgar https://codeberg.org/maxagahi/homebrew-yadgar` then `brew install yadgar`. |
| `.forgejo/workflows/ci.yaml` (in tap repo) | Validates formula syntax via `brew audit --strict` on PR. |

### Modified files (in yadgar repo)

| Path | Change |
|---|---|
| `pyproject.toml` | Fix license classifier mismatch (Apache-2.0). Add missing classifiers: `Topic :: System :: Filesystems`, `Operating System :: POSIX :: Linux`, `Operating System :: MacOS`, `Environment :: Console`. Add `cyclonedx-py` to `[dev]` extras. Add `[project.optional-dependencies.sbom]` group (lightweight: just `cyclonedx-py`). |
| `.forgejo/workflows/ci.yaml` | Existing `publish` job stays. Either: (a) extend with release-asset steps, or (b) extract release-asset generation to NEW `.forgejo/workflows/release.yaml` (decoupled, simpler). Lean: (b). Decide in Step 0. |
| `MIGRATION_NOTES.md` | v5.46.0 section: new install paths + bump script semantics + SBOM availability. |
| `README.md` | Add Homebrew + Nix install commands to the Quick Start section. Cross-reference `docs/INSTALL.md`. |
| `CHANGELOG.md` | v5.46.0 entry. |
| `server.json` | Version bump 5.45.0 → 5.46.0 via `scripts/bump_version.py`. |
| `LICENSE` | Verify content matches `pyproject.toml` `license = "Apache-2.0"`. Read in Step 0 before classifier fix. |

---

## Homebrew formula skeleton

`Formula/yadgar.rb.in` — placeholders `@VERSION@`, `@SHA256@`, `@PYTHON_VERSION@`:

```ruby
class Yadgar < Formula
  include Language::Python::Virtualenv

  desc "Persistent memory engine for Claude Code"
  homepage "https://codeberg.org/maxagahi/yadgar"
  url "https://files.pythonhosted.org/packages/source/y/yadgar/yadgar-@VERSION@.tar.gz"
  sha256 "@SHA256@"
  license "Apache-2.0"

  depends_on "python@@PYTHON_VERSION@"
  depends_on "podman" => :recommended
  # docker as optional alternate runtime
  depends_on "docker" => :optional

  def install
    virtualenv_install_with_resources
  end

  def post_install
    ohai "Run `yadgar install` to complete setup (interactive)."
    ohai "Or `yadgar install --non-interactive` for CI environments."
  end

  test do
    assert_match "yadgar #{version}", shell_output("#{bin}/yadgar --version")
  end
end
```

**Python version fallback strategy** (open question 1 below): if `python@3.14` not yet available on Homebrew core (per `brew search python@`), use `python@3.13` + add a comment in the formula explaining the lag. Confirm at implementation time.

---

## Nix flake outputs spec

`flake.nix` produces:

```nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };
  outputs = { self, nixpkgs, flake-utils }: flake-utils.lib.eachDefaultSystem (system:
    let pkgs = nixpkgs.legacyPackages.${system}; in {
      packages = {
        yadgar = pkgs.python314Packages.buildPythonPackage { ... };  # builds from pyproject.toml
        default = self.packages.${system}.yadgar;
      };
      apps.yadgar = {
        type = "app";
        program = "${self.packages.${system}.yadgar}/bin/yadgar";
      };
      apps.default = self.apps.${system}.yadgar;
    }) // {
    nixosModules.yadgar = import ./nix/modules/nixos.nix;
    homeManagerModules.yadgar = import ./nix/modules/home-manager.nix;
  };
}
```

`nix/modules/home-manager.nix` mirrors the current `~/git/nix/modules/home/yadgar.nix` (1300 lines) — moved into this repo. The nix-repo file stays for now as a backward-compat shim that imports from the flake (`import (fetchTarball "...") {}.homeManagerModules.yadgar`).

Migration of existing NixOS users from nix-repo → yadgar flake is opt-in; no breakage.

---

## Codeberg release automation flow

Trigger: `git push origin v5.46.0` (or any `v*` tag).

`.forgejo/workflows/release.yaml` jobs:

1. **`build-sdist`** — `python -m build --sdist`. Output: `dist/yadgar-<version>.tar.gz`.
2. **`gen-sbom`** — `cyclonedx-py environment > dist/yadgar-<version>-sbom.cdx.json` (after `pip install -e .`).
3. **`gen-checksums`** — `sha256sum dist/* > dist/CHECKSUMS.txt`.
4. **`attach-to-release`** — Forgejo API: create release for tag, upload all `dist/*` as release assets.
5. **`open-brew-pr`** — Auto-clone `homebrew-yadgar`, render `Formula/yadgar.rb` from template, push to a branch named `bump-v<version>`, open PR via Forgejo API.
6. **`open-nix-pr`** — Auto-clone `nix` repo (max's personal), bump `yadger_core_version` in `modules/home/yadgar.nix`, push to branch `bump-yadgar-v<version>`, open PR.

Jobs 5 + 6 require a Forgejo access token with PR-create scope on the respective repos. Store as `BREW_BUMP_TOKEN` + `NIX_BUMP_TOKEN` repo secrets.

`publish` job (existing in `ci.yaml`) remains the PyPI publish step — unchanged.

---

## SBOM format

**Default:** CycloneDX JSON 1.5 — via `cyclonedx-py environment` (recommended over `cyclonedx-py poetry` since yadgar uses hatchling, not poetry).

**Why CycloneDX:** maintained by OWASP, broadly accepted by enterprise security scanners (Trivy, Grype, Anchore).

**Future variant:** SPDX JSON via `spdx-tools` (Python). Deferred to v5.47+ if user demand surfaces.

SBOM is attached to each release as `yadgar-<version>-sbom.cdx.json`. Not embedded in the wheel or container image (size + reproducibility concerns).

---

## Open questions (must resolve during implementation)

1. **Python 3.14 availability on Homebrew core.** `pyproject.toml` requires `python>=3.14`. Homebrew may lag (typical 1-2 month delay for new minor versions). **Action at implementation:** check `brew info python@3.14` — if missing, fall back to `python@3.13` in the formula + document the lag in `docs/INSTALL.md`. Re-evaluate per release.

2. **License classifier mismatch.** Current `pyproject.toml`:
   ```
   license = "Apache-2.0"   # SPDX expression
   classifiers = [ ..., "License :: OSI Approved :: MIT License", ... ]
   ```
   These contradict. **Action:** read `LICENSE` file in Step 0 → fix to match. Almost certainly Apache-2.0 wins (matches the SPDX expression which is the modern canonical field).

3. **Bump script source-of-truth scope.** Should `scripts/bump_version.py` update the homebrew-yadgar tap formula directly (via cross-repo git), or only generate the formula content for the release workflow to PR? Lean: only generate — let the release workflow do the cross-repo PR. Keeps bump script offline-safe + scriptable for dev use.

4. **NixOS module path.** Should the current `~/git/nix/modules/home/yadgar.nix` (1300 lines) be moved into the yadgar repo, OR mirrored, OR kept separate with a fetchTarball shim? Lean: move into yadgar repo at `nix/modules/home-manager.nix`, leave a thin shim in nix repo. User manages nix repo personally; cross-repo coupling is acceptable.

5. **Tag pattern for release workflow.** `v*` matches `v5.46.0` AND alpha/beta tags (`v5.46.0-alpha.1`). Should pre-release tags trigger release automation? Lean: yes — pre-releases publish to Codeberg + PyPI as pre-release (PyPI auto-detects `5.46.0a1`), but skip brew/nix PR auto-open. Conditional: `if: !contains(github.ref, '-alpha') && !contains(github.ref, '-beta')`.

6. **Signed release artifacts (sigstore / cosign).** Out of scope for v5.46. Document as deferred to v5.47+ candidate.

---

## Plan steps (concrete, executable)

### Step 0 — Pre-flight (≤ 0.25 day)

- Read `LICENSE` file. Confirm Apache-2.0 vs MIT mismatch resolution.
- Check `brew search python@3.14` (or `brew info python@3.14`) — confirm or fall back to 3.13.
- Verify `cyclonedx-py` works on a clean venv: `pip install cyclonedx-py && cyclonedx-py environment --output-format=json --schema-version=1.5 --output-file=/tmp/sbom.json`.
- Confirm Forgejo Actions support `actions/create-release` equivalent — check `https://data.forgejo.org/actions` for release-create action, or use `curl` against Forgejo REST API.
- Confirm `https://files.pythonhosted.org/packages/source/y/yadgar/yadgar-<version>.tar.gz` URL pattern works (PyPI publish must complete before release workflow runs).

### Step 1 — TDD scaffolding (≤ 0.5 day)

Tests under `yadgar/tests/test_distribution.py` (new file):

- `scripts/bump_version.py 5.45.0 5.46.0` updates pyproject.toml, server.json, flake.nix consistently.
- `scripts/bump_version.py` refuses on invalid semver.
- `scripts/gen_sbom.sh` produces valid CycloneDX 1.5 JSON (validate via `python -c "import json; json.load(open(...))"` + schema check).
- `Formula/yadgar.rb.in` placeholder substitution: render with mock version + sha256 → output matches expected.
- License classifier in pyproject.toml is `License :: OSI Approved :: Apache Software License`.
- `flake.nix` parses (via `nix-instantiate --parse flake.nix` if `nix` available in CI; else skip).

### Step 2 — pyproject.toml polish (≤ 0.25 day)

- Fix license classifier (Apache-2.0).
- Add missing OS / Topic / Environment classifiers.
- Add `cyclonedx-py>=4.0` to `[project.optional-dependencies].dev`.
- Add new optional-dependency group `[project.optional-dependencies].sbom = ["cyclonedx-py>=4.0"]` for users who want just SBOM tooling.

### Step 3 — Bump script + version sync (≤ 0.5 day)

- Create `scripts/bump_version.py`. Args: `<old> <new>`. Edits: `pyproject.toml`, `server.json` (`version` + `backend_version` if both bump), `flake.nix` (`version` attr).
- Generates content for `Formula/yadgar.rb` + outputs to stdout (for release workflow to consume).
- Idempotent: re-running with same args is a no-op.
- Refuses if uncommitted changes in working tree (safety).
- Existing `scripts/check_versions.py` (referenced in I25) confirms sync; bump script + check script work together.

### Step 4 — flake.nix + nix modules in repo (≤ 1 day)

- Create `flake.nix` + `flake.lock` (run `nix flake lock`).
- Move `~/git/nix/modules/home/yadgar.nix` → `nix/modules/home-manager.nix` in yadgar repo. Adjust for flake-input paths (replace `homeDir` references, etc — minimal changes).
- Add `nix/modules/nixos.nix` — system-level service variant (if user demand; can be a stub in v5.46 + filled in v5.46.1).
- Add backward-compat shim in `~/git/nix/`: `modules/home/yadgar.nix` imports `(fetchTarball ".../codeberg/yadgar/archive/v5.46.0.tar.gz").homeManagerModules.yadgar`. This is a NIX-REPO change — handed to user via MIGRATION_NOTES, NOT auto-applied (hard rule: no auto-apply infra changes).
- Validate via `nix flake check` (if available).

### Step 5 — Homebrew tap (≤ 0.5 day)

- Create `homebrew-yadgar` repo on Codeberg (manual; gh + Forgejo equivalent). User-action: documented in MIGRATION_NOTES.
- Add `Formula/yadgar.rb` (initial; placeholder version + sha256).
- Add `README.md` for tap.
- Add `.forgejo/workflows/ci.yaml` in tap repo: validate formula syntax on PR.
- In yadgar repo: create `Formula/yadgar.rb.in` template.

### Step 6 — SBOM generation (≤ 0.25 day)

- Create `scripts/gen_sbom.sh`. Wraps `cyclonedx-py environment`.
- Validates output against CycloneDX 1.5 schema.
- Writes to `dist/yadgar-<version>-sbom.cdx.json` (version read from `pyproject.toml`).

### Step 7 — Release workflow (≤ 1 day)

- Create `.forgejo/workflows/release.yaml`.
- Triggers on `tags: v*`.
- Jobs:
  - `build-sdist` (needs PyPI publish to complete first — depends on existing `publish` job in `ci.yaml`; use `needs:` cross-workflow dependency, OR move all release logic into ci.yaml as new jobs).
  - `gen-sbom`
  - `gen-checksums`
  - `attach-to-release` (via Forgejo REST API + `curl`)
  - `open-brew-pr` (conditional: skip pre-release tags)
  - `open-nix-pr` (conditional: skip pre-release tags)
- Configure repo secrets: `BREW_BUMP_TOKEN`, `NIX_BUMP_TOKEN` (user-managed; documented in MIGRATION_NOTES).

### Step 8 — docs/INSTALL.md (≤ 0.25 day)

- Per-platform install: PyPI / Homebrew / Nix flake / source.
- For each: install command + post-install `yadgar install` reference.
- NixOS users: link to flake + home-manager module example.
- macOS users: link to Homebrew tap.

### Step 9 — Version bump + CHANGELOG + MIGRATION_NOTES (≤ 0.25 day)

- `scripts/bump_version.py 5.45.0 5.46.0`.
- CHANGELOG.md entry.
- MIGRATION_NOTES.md v5.46.0 section: new install paths + bump script semantics + SBOM availability + secrets to add to Codeberg repo.

---

## Acceptance criteria

v5.46.0 ships when ALL of the following are true:

- [ ] PyPI publish for v5.46.0 succeeds via existing `publish` job.
- [ ] `pip install yadgar==5.46.0` works on a clean venv with Python 3.14.
- [ ] `pyproject.toml` license classifier matches `LICENSE` file (Apache-2.0).
- [ ] `Formula/yadgar.rb` in `homebrew-yadgar` tap passes `brew audit --strict yadgar`.
- [ ] `brew install maxagahi/yadgar/yadgar` succeeds on macOS (manual smoke test).
- [ ] `nix run codeberg:maxagahi/yadgar#yadgar -- --version` outputs `yadgar 5.46.0`.
- [ ] `nix flake check` exits 0.
- [ ] `homeManagerModules.yadgar` from flake imports cleanly into a test home-manager config.
- [ ] Release workflow on `v5.46.0` tag attaches: sdist, SBOM, CHECKSUMS to Codeberg release.
- [ ] SBOM is valid CycloneDX 1.5 (validate via `cyclonedx-py validate`).
- [ ] Brew bump PR auto-opened against `homebrew-yadgar` (manual merge required).
- [ ] Nix bump PR auto-opened against `nix` repo (manual merge required).
- [ ] `scripts/bump_version.py` round-trip: bump 5.45→5.46→5.45→5.46 yields no diff at each end.
- [ ] CHANGELOG.md v5.46.0 entry exists.
- [ ] `docs/INSTALL.md` exists with all four install paths documented.
- [ ] MIGRATION_NOTES.md v5.46.0 section documents new install paths + bump script + SBOM + required Codeberg secrets.
- [ ] `python scripts/check_versions.py` exit 0.

**NOT in scope:** signed artifacts, Windows installer, SPDX SBOM, alternate container registries.

---

## Effort estimate (calendar days)

| Step | Days |
|---|---:|
| Step 0 pre-flight | 0.25 |
| Step 1 TDD scaffolding | 0.5 |
| Step 2 pyproject.toml polish | 0.25 |
| Step 3 bump script | 0.5 |
| Step 4 flake.nix + nix modules | 1 |
| Step 5 Homebrew tap | 0.5 |
| Step 6 SBOM generation | 0.25 |
| Step 7 release workflow | 1 |
| Step 8 docs/INSTALL.md | 0.25 |
| Step 9 bump + CHANGELOG + MIGRATION_NOTES | 0.25 |
| **Total** | **3 – 4 calendar days** |

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Python 3.14 not available on Homebrew core at release time | Fall back to `python@3.13` in formula + document. Re-bump formula when 3.14 lands. |
| `cyclonedx-py` dependency tree conflicts with yadgar deps | Isolated to `[sbom]` extras. SBOM generation happens in release workflow only, not user runtime. |
| Forgejo Actions API for release-asset upload differs from GitHub | Confirmed in Step 0 pre-flight via Forgejo REST API direct call. Fallback: use `curl` + Forgejo `/repos/{owner}/{repo}/releases/{id}/assets` endpoint. |
| Cross-repo PR auto-open (`open-brew-pr`, `open-nix-pr`) fails silently | Each job emits explicit success/failure log. If PR-open fails, release still ships (assets uploaded); user manually bumps brew/nix formulas. Document fallback in MIGRATION_NOTES. |
| `nix flake check` fails on first invocation due to missing nixpkgs commit | `flake.lock` pins commit; `nix flake check` uses lockfile. Validate in Step 1. |
| home-manager module migration breaks existing nix-managed users | Backward-compat shim in nix-repo (handed to user via MIGRATION_NOTES). User-action; no auto-apply. |
| Homebrew tap PR auto-open requires SSH key or PAT — security concern | Use Forgejo PAT scoped to PR-create on tap repo only. Document rotation policy in MIGRATION_NOTES. |
| Bump script desync across repos (yadgar bumps but brew/nix PRs not yet merged) | Release workflow continues regardless; users on brew/nix will see lag until PRs merge. Documented as expected behavior. |
| pre-release tags (alpha/beta) accidentally trigger brew/nix bump PRs | Conditional in release.yaml: `if: !contains(github.ref, '-alpha') && !contains(github.ref, '-beta')`. |

---

## Dependencies + blockers

- **Depends on v5.45.0 shipped** — install asset layout (`yadgar/install_assets/`) + multi-OS install paths must exist for Homebrew formula's `post_install` to suggest `yadgar install`.
- **Blocks v5.47.0** — update mechanism needs install-method detection (`pipx` / `brew` / `nix-flake` / `container`), which v5.46 makes a reality.
- **No external dependencies** on PyPI / Homebrew / Nix infrastructure beyond standard public APIs.

---

## TDD test list

Under `yadgar/tests/test_distribution.py` (new file). Markers: `not integration` (no live tag pushes).

1. `test_bump_version_updates_pyproject` — `bump_version.py 5.45.0 5.46.0` writes new version in pyproject.toml.
2. `test_bump_version_updates_server_json` — same for server.json.
3. `test_bump_version_updates_flake_nix` — same for flake.nix version attr.
4. `test_bump_version_refuses_invalid_semver` — non-semver input → exit non-zero.
5. `test_bump_version_refuses_uncommitted_changes` — dirty working tree → exit non-zero.
6. `test_bump_version_idempotent` — run twice with same args → no change second time.
7. `test_gen_sbom_produces_valid_cyclonedx` — generate SBOM, validate against schema.
8. `test_brew_formula_template_renders` — render with mock version/sha256 → matches expected content.
9. `test_brew_formula_uses_python_3_14_or_fallback` — generated formula references python@3.14 OR python@3.13 (whichever available; one of the two is asserted).
10. `test_pyproject_license_classifier_matches_license_file` — read LICENSE file + assert classifier consistency.
11. `test_pyproject_has_required_classifiers` — assert presence of Linux + macOS + Console + Filesystems classifiers.
12. `test_flake_nix_parses` — `nix-instantiate --parse flake.nix` exit 0 (skipped if nix unavailable).
13. `test_release_workflow_has_pre_release_skip` — parse release.yaml YAML, assert brew/nix bump jobs have `if: !contains(github.ref, '-alpha')` condition.

---

## Coordination notes for main thread

- Plan-only doc → direct to master per workflow rule (wiki slug `yadgar-workflow-plan-commits-direct-to-master`).
- Implementation work requires a feature branch — `feat/v5.46.0-distribution`. Branch from latest master after v5.45.0 merges.
- Cross-repo work required: `homebrew-yadgar` (new repo) + `nix` (existing) — both need PR-create tokens stored as Codeberg repo secrets.
- NixOS module migration is opt-in for existing users; backward-compat shim documented in MIGRATION_NOTES.
- Related plans: `docs/PLAN_V5_45_0_SETUP_FOUNDATION.md` (prerequisite) + `docs/PLAN_V5_47_0_UPDATE_MECHANISM.md` (downstream).
- Implementer must read `docs/DECISIONS.md` PD-37 before re-scoping any distribution choice.
