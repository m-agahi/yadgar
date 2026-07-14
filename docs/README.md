# Yadgar docs

Taxonomy index for the `docs/` tree. Every doc lives under a category dir by role.
ADRs are **not** files here — they are wiki-native (`wiki:yadgar-adr-log` + the
`adr_add` MCP tool; see `contracts/ARCHITECTURE_INVARIANTS.md` §I34 / ADR-0062).

## Layout

```
docs/
├── README.md              ← this index
├── CHANGELOG.md           ← release history (root, keep-uppercase — frozen; never rewrite historical paths)
├── contracts/             ← enforcement source-of-truth (SCREAMING_SNAKE, path-pinned in pre-commit)
├── reference/             ← stable how-it-works / how-to docs (kebab-case)
├── benchmark-results/     ← BENCHMARK_RESULTS + BENCHMARK_LICENSE
├── testing/               ← test runbooks + perf breakdowns
├── reports/
│   ├── ci/                ← CI incident / speedup reports (dated)
│   ├── audits/            ← competitor / license / complexity / audit reports (dated)
│   └── releases/          ← per-release snapshots + design reports (dated)
├── plans/                 ← open plans (slug-dated) + ROADMAP.md index; archive/ frozen
├── roadmap/               ← workflow-roadmap-update.md (RMW recipe for the roadmap wiki)
│   └── archive/           ← historical per-version roadmaps (v4.8…v7) — frozen
├── diagrams/              ← YAML-driven diagram generator; out/ is generated + git-ignored (#68)
├── observability/         ← Prometheus alerts + Grafana dashboard
├── maintainer-notes/      ← nix-integration, todo
└── assets/                ← shared images (yadgar.svg)
```

## Naming convention

| Category | Dir | Naming |
|---|---|---|
| Contracts | `contracts/` | `SCREAMING_SNAKE.md` (referenced by exact basename; path-pinned in `.pre-commit-config.yaml` + lint scripts + tests) |
| Reference | `reference/` | `kebab-case.md` |
| Reports | `reports/{ci,audits,releases}/` | `<topic>-YYYY-MM-DD.md` (or `<topic>-vX-Y-Z.md` for release-keyed) |
| Plans | `plans/` | slug-named (never version-named); see `plans/ROADMAP.md` convention |
| Roadmap history | `roadmap/archive/` | `vN.md` (frozen) |

## Roadmap sources (three distinct roles — do NOT merge)

Yadgar has three roadmap-flavoured sources; they are **not** copies of each other:

1. **`plans/ROADMAP.md`** — the **open-plans index / plans SSOT**. Lists every
   active plan doc. Not a version roadmap. This is where new plans register.
2. **`roadmap/archive/vN.md`** — **historical per-version plans** (v4.8…v7),
   frozen record of what each version scoped at the time. Reference only.
3. **`wiki:yadgar-roadmap-future-improvements`** — the **agent-facing forward
   roadmap** (version-execution order + future improvements), maintained in the
   wiki. This is the single canonical *forward* roadmap. (The former file mirror
   `docs/yadgar-roadmap-future-improvements.md` was deleted in the 2026-07-14
   docs-reorg — the wiki page now stands alone.)

## ADRs

Architecture Decision Records are wiki-native, not files under `docs/`. Read via
`wiki:yadgar-adr-log`; create with the `adr_add` MCP tool. See
`contracts/ARCHITECTURE_INVARIANTS.md` §I34 (ADR-0062).
