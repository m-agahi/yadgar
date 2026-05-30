# Yadgar Versioning Convention

**Set:** 2026-05-30. Applies to all new version slots from this date forward.

---

## Strict semver — vX.Y.Z only

Yadgar uses **three-part semver `vX.Y.Z`** exclusively. Four-digit suffixes (e.g. `v5.10.7.1`) were grandfathered for pre-convention hotfixes (see below) and are not permitted for new releases.

| Part | Meaning | When to bump |
|---|---|---|
| **X** (major) | Breaking changes / huge architectural shifts | Breaking API changes; v6.0.0 = two-tier model routing |
| **Y** (minor) | Each new feature or topical change | Every new feature, new primitive, new integration — one minor per feature |
| **Z** (patch) | Hotfixes only | Regressions or ship issues that emerged after the minor shipped |

---

## Rules

### New feature mid-flight

If a plan is drafted under an old number and the convention changes before it ships, renumber to the next available minor **before the first implementation commit**. Document the rename in the plan header with a `**Renumbered:**` line:

```
**Renumbered:** vOLD → vNEW on YYYY-MM-DD. Reason: skip-1 minor convention adopted; odd-only minors for sequential features.
```

### Hotfix dispatch

When a ship issue emerges after a minor lands:

1. Assign the next patch: `vX.Y.1` (or `.2`, `.3`, ...).
2. Plan file name: `PLAN_V5_Y_Z_DESCRIPTION.md` (underscore-separated).
3. Keep the parent minor's plan file unchanged; create a new plan file for the patch.

### New feature after prior minor — skip-1 convention

Each new feature = new minor. Do not chain feature work into a patch slot. Under the **skip-1 convention** (adopted 2026-05-30), consecutive features use **odd-only minors** (v5.11, v5.13, v5.15, ...) to leave the even slot between them as hotfix/patch space.

- The skipped even minor is NOT pre-reserved — it is simply left free so a hotfix patch train can land between two features without forcing a major renumber cascade.
- If no hotfix arrives between two features, the even slot remains unused. Gaps are intentional and acceptable.

Example:
- v5.11.0 ships (viz config).
- Bug found in v5.11.0: → v5.11.1 hotfix.
- Next new feature: → v5.13.0 (not v5.12.0, which is even/skipped).
- Bug found in v5.13.0: → v5.13.1.
- Next new feature: → v5.15.0.

### Gaps in minor numbering

Gaps are intentional and acceptable. Even minors are skipped by the skip-1 convention. Deferred plans may hold far-future slots (e.g. v5.99.0 for roadmap freshness). Do not renumber deferred slots to close gaps.

### Plan file naming

```
docs/PLAN_V{MAJOR}_{MINOR}_{PATCH}_{DESCRIPTION_CAPS}.md
```

Examples:
- `docs/PLAN_V5_25_0_BENCHMARK_PUBLICATION.md`
- `docs/PLAN_V5_25_1_LOCOMO_HOTFIX.md` (hypothetical patch)
- `docs/PLAN_V5_27_0_DUCKDB_EXPORT.md`

### DECISIONS.md version slot field

When updating the R2/D2/D3 revisit-trigger or version-slot field in `docs/DECISIONS.md`, use the concrete version number, not `vX.Y.x`. Example: `v5.31.0` not `v5.31.x`.

---

## Examples: right vs wrong

| Scenario | Right | Wrong |
|---|---|---|
| New feature after v5.10.7.3 | v5.11.0 | v5.10.8, v5.10.8.0 |
| Hotfix to v5.11.0 bug | v5.11.1 | v5.11.0.1, v5.12.0 |
| Next feature after v5.11.0 (skip-1) | v5.13.0 | v5.12.0 (even slot left for hotfix gap) |
| Hotfix to v5.13.0 bug | v5.13.1 | v5.14.0 |
| Next feature after v5.13.0 (skip-1) | v5.15.0 | v5.14.0 (even), v5.13.2 (wrong tier) |
| Deferred far-future plan | v5.99.0 (gap intentional) | v5.38.0 (too close to active pipeline) |
| Breaking architectural change | v6.0.0 | v5.99.0 |

---

## Grandfathering: pre-2026-05-30 four-digit tags

The following tags shipped before this convention was adopted. They are **historical record** and must not be re-tagged or removed:

| Tag | Release | Notes |
|---|---|---|
| `v5.10.7.1` | 2026-05-30 | SessionEnd sentinel filter (local-command tags) |
| `v5.10.7.2` | 2026-05-30 | 3D viz lighting fix (MeshLambertMaterial → MeshBasicMaterial) |
| `v5.10.7.3` | 2026-05-30 | Revert v5.10.7 custom 3D node geometry to ForceGraph3D defaults |

These are the only permitted four-digit version tags. Any future tag with four parts is a convention violation.

---

## Renumber history (2026-05-30) — strict semver adoption

On 2026-05-30 all drafted (unshipped) plan slots were renumbered from pre-convention patch-train slots to comply with strict semver (one minor per feature). The mapping:

| Old slot | New slot | Plan |
|---|---|---|
| v5.10.8 | v5.11.0 | Secret-gate context awareness + allowlist |
| v5.10.9 | v5.12.0 | CPU bursts residual + detection infra |
| v5.10.10 | v5.13.0 | Write-time contradiction detection |
| v5.10.12 | v5.14.0 | Anchor unconditional surfacing |
| v5.11.0 | v5.15.0 | Anchor cross-project dedup + Jira |
| v5.12.0 | v5.16.0 | Wiki bookmarks page in viz |
| v5.13.0 | v5.17.0 | Benchmark publication (LongMemEval) |
| v5.13.1 | v5.18.0 | DuckDB analytics export |
| v5.13.2 | v5.19.0 | Bi-temporal edges extension |
| v5.14.0 | v5.20.0 | Recall pipeline plugin architecture (R2) |
| v5.14.1 | v5.21.0 | In-context memory blocks (Letta-style) |
| v5.15.0 | v5.22.0 | JavaScript / TypeScript SDK |
| v5.20.0 | v5.30.0 | Roadmap freshness (DEFERRED) |

The v5.10.11 free slot (reclaimed from YM-W-6 security closure) was dropped; no plan existed there.

---

## Renumber history (2026-05-30) — skip-1 minor convention adoption

Same date, second pass. v5.11.0 slot pre-empted by new viz config.yaml plan, triggering adoption of the **skip-1 minor convention**: consecutive features occupy odd-only minors, leaving even slots as hotfix gap space.

All drafted plan slots cascaded from the first-pass (strict semver) numbering to the skip-1 numbering:

| First-pass slot | Skip-1 slot | Plan |
|---|---|---|
| v5.11.0 (viz config.yaml) | **v5.11.0** — no change (first odd slot) | Viz knobs configurable via config.yaml (NEW) |
| v5.11.0 (secret-gate) | **v5.13.0** | Secret-gate context awareness + allowlist |
| v5.12.0 | **v5.15.0** | CPU bursts residual + detection infra |
| v5.13.0 | **v5.17.0** | Adopt-2 write-time contradiction detection |
| v5.14.0 | **v5.19.0** | Anchor unconditional surfacing |
| v5.15.0 | **v5.21.0** | Anchor cross-project dedup + Jira |
| v5.16.0 | **v5.23.0** | Wiki bookmarks page in viz |
| v5.17.0 | **v5.25.0** | Benchmark publication (LongMemEval) |
| v5.18.0 | **v5.27.0** | DuckDB analytics export |
| v5.19.0 | **v5.29.0** | Bi-temporal edges extension |
| v5.20.0 | **v5.31.0** | Recall pipeline plugin architecture (R2) |
| v5.21.0 | **v5.33.0** | In-context memory blocks (Letta-style) |
| v5.22.0 | **v5.35.0** | JavaScript / TypeScript SDK |
| v5.23.0 | **v5.37.0** | Viz integration testing (Playwright + API) |
| v5.30.0 | **v5.99.0** | Roadmap freshness (DEFERRED — far future) |

---

## Cross-reference

- `docs/DECISIONS.md` — convention for how and when to update version slots in the decisions log.
- `docs/CHANGELOG.md` — all shipped versions (historical record, never rewritten).
- `docs/PLAN_V*.md` — plan files follow the naming convention above.
- Wiki page `yadgar-versioning-convention` — mirrors this document for cross-session search.
