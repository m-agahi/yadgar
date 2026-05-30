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
**Renumbered:** vOLD → vNEW on YYYY-MM-DD. Reason: strict semver convention adopted; all new features get minor bumps.
```

### Hotfix dispatch

When a ship issue emerges after a minor lands:

1. Assign the next patch: `vX.Y.1` (or `.2`, `.3`, ...).
2. Plan file name: `PLAN_V5_Y_Z_DESCRIPTION.md` (underscore-separated).
3. Keep the parent minor's plan file unchanged; create a new plan file for the patch.

### New feature after prior minor

Each new feature = new minor. Do not chain feature work into a patch slot.

Example:
- v5.17.0 ships (benchmarks).
- Bug found in v5.17.0: → v5.17.1 (hotfix).
- Next new feature: → v5.18.0 (not v5.17.2).

### Gaps in minor numbering

Gaps are intentional and acceptable. When a deferred plan is assigned a far-future slot (e.g. v5.30.0 for roadmap freshness), intermediate slots remain free for features that arrive before the deferred plan is picked up. Do not renumber deferred slots to close gaps.

### Plan file naming

```
docs/PLAN_V{MAJOR}_{MINOR}_{PATCH}_{DESCRIPTION_CAPS}.md
```

Examples:
- `docs/PLAN_V5_17_0_BENCHMARK_PUBLICATION.md`
- `docs/PLAN_V5_17_1_LOCOMO_HOTFIX.md` (hypothetical patch)
- `docs/PLAN_V5_18_0_DUCKDB_EXPORT.md`

### DECISIONS.md version slot field

When updating the R2/D2/D3 revisit-trigger or version-slot field in `docs/DECISIONS.md`, use the concrete version number, not `vX.Y.x`. Example: `v5.20.0` not `v5.20.x`.

---

## Examples: right vs wrong

| Scenario | Right | Wrong |
|---|---|---|
| New feature after v5.10.7.3 | v5.11.0 | v5.10.8, v5.10.8.0 |
| Hotfix to v5.11.0 bug | v5.11.1 | v5.11.0.1, v5.12.0 |
| Next feature after v5.11.0 | v5.12.0 | v5.11.1 (reserved for hotfix) |
| Deferred far-future plan | v5.30.0 (gap intentional) | v5.12.1 (wrong tier), v5.13.0 (too close) |
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

## Renumber history (2026-05-30)

On 2026-05-30 all drafted (unshipped) plan slots were renumbered to comply with this convention. The mapping:

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

## Cross-reference

- `docs/DECISIONS.md` — convention for how and when to update version slots in the decisions log.
- `docs/CHANGELOG.md` — all shipped versions (historical record, never rewritten).
- `docs/PLAN_V*.md` — plan files follow the naming convention above.
- Wiki page `yadgar-versioning-convention` — mirrors this document for cross-session search.
