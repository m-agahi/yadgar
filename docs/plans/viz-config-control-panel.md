# viz-config-control-panel

**Status:** skeleton — design proposed, not started. Discuss before building.
**Theme:** viz / config / ops
**Target:** unassigned (version stamped at ship)
**Owner:** —
**Depends on:** existing viz REST backend (`yadgar/graph_api.py` + `yadgar/server/http.py`), `/admin/config` endpoint (`yadgar/server/admin_config.py`), config three-way-sync (I25).

---

## Problem / motivation

This session the user had to flip `COLD_MEMORY_PURGE_ENABLED` via `yadgar config set`
on the CLI, then manually restart the daemons for it to take effect. Every config
change today is: drop to a shell → `yadgar config set` → remember which daemon reads
that knob → restart it → hope you didn't typo a float threshold. There are **299
user-facing settings** (`yadgar config list`; 300 lines incl. header) — far too many
to hold in the head, and the CLI gives no
grouping, no per-setting description, no "does this need a restart" signal, and no
guardrail on the destructive ones.

The viz UI already renders a 3D memory graph (Starlette REST backend in
`graph_api.py` / `server/http.py`, plus a frontend). It is the natural home for a
**config control panel**: view every setting with its live value + source, search/
filter/group them, edit the safe ones inline, and — critically — surface *which*
changes need a daemon restart and offer a guarded restart action, plus an armed
two-step confirm for destructive knobs. Same sanctioned write path as the CLI; no
yaml hand-editing.

---

## Control surface (enumeration + per-setting metadata)

### What to enumerate — all 299, via `config list`
- **The panel enumerates all 299 `Settings` fields** — exactly what
  `yadgar config list` shows (iterates `Settings.model_fields` in
  `config_yaml.py::cmd_config_list`), each with its resolved value + source.
- The 299 split into two populations (they do **not** nest — the registry is *not* a
  superset of the 299):
  - **~175 yaml-backed knobs** — present in **both** `config_registry.py::_REGISTRY`
    (`grep -c "ConfigEntry(" config_registry.py` → 175) **and** `FIELD_META`. These
    are editable: a yaml write actually changes them.
  - **~124 env-only knobs** — listed in `yadgar/tests/config_env_only_allowlist.txt`
    (secrets, infra-wiring, bootstrap paths, deployment flags). A yaml write can never
    override these. They render **read-only / locked** with an `ENV` (or whatever the
    resolved source is) badge, never as editable cards; the PATCH path refuses them.
  - (299 ≈ 175 + 124; the I25 invariant is exactly that every `Settings` field lands
    in one population or the other.)
- The earlier scoping note's "~174 ConfigEntry" was correct (175 today); the
  "321-row registry superset" figure was a miscount (a line-range end mistaken for a
  row count) — **do not use it.**

### Per-setting metadata (the shape the UI needs)
Today `/admin/config` (`build_config_table()`) returns per knob:
`{name, value, source, kind}` where `kind ∈ {int,float,bool,string}` and
`source ∈ {env, yaml, default}`. The UI needs **four more fields**:

| field | source today | status |
|-------|--------------|--------|
| `name` | registry / `Settings` | exists |
| `value` (current effective) | resolved env>yaml>default | exists |
| `source` (`env`/`yaml`/`default`) | `cmd_config_list` resolution | exists |
| `kind` (`int`/`float`/`bool`/`string`) | `ConfigEntry.kind` | exists |
| `description` | `config_yaml.py::FIELD_META[key].desc` | exists, not yet in the table |
| `category` | mapped from `FIELD_META[key].section` (see below) | derived, net-new mapping |
| `restart_required` | **does not exist anywhere** | **net-new metadata** |
| `destructive` | **does not exist anywhere** | **net-new metadata** |
| `enum_choices` (optional, for string knobs with a fixed set) | none today | net-new, optional |

### Category — the rail vocabulary
- The **left rail uses the 15 CAPABILITY categories** (canonical vocabulary in
  `docs/CAPABILITY_REGISTRY.md`): `retrieval, storage, gate, consolidation,
  enrichment, wiki, curation, ops, viz, security, observability, brain-dynamics,
  mcp-tool, write-path, config`.
- **`FIELD_META.section` is NOT this vocabulary.** Sections are
  `core / logging / retrieval_fusion / viz_config / …` — a finer, separate grouping.
  We need a **section→category map** (a small dict, ~30 sections → 15 categories,
  reviewed once, kept beside `FIELD_META`). A knob whose section is unmapped falls
  back to category `config`. This map is the single coupling between the two
  vocabularies and must be reviewed when a new section is introduced.
- **Decision:** rail = CAPABILITY categories (matches the rest of the codebase's
  mental model and the user's request); section is an optional sub-group label
  inside a category. Lock this so the plan's frontend spec and the mockup agree.

### Where restart_required / destructive live (I25 impact — decide explicitly)
Two options; **plan recommends Option A**:

- **Option A — new `ConfigEntry` fields** (`restart_required: bool = True`,
  `destructive: bool = False`). Pros: single source of truth, travels with the knob,
  shows up in `as_dict()` for both CLI and API. Cons: **touches the three-way-sync
  surface** — every registry row gains two fields; `test_config_three_way_sync.py`
  and any registry-shape assertions must be updated, and the default (`restart_required=True`)
  must be conservative so an un-annotated knob is treated as restart-needed (fail safe).
- **Option B — sidecar annotation dict** (`CONFIG_RUNTIME_META: dict[name, {restart_required, destructive}]`
  beside `FIELD_META`). Pros: zero change to `ConfigEntry`/registry shape → **no I25
  test churn**. Cons: a fourth place to keep in sync; drift risk (a knob in the
  registry but not the sidecar). Mitigate with a test asserting every editable knob
  has a sidecar entry.

Recommend **A** for single-source-of-truth, accept the I25 test update as part of P3.
Default `restart_required=True` (conservative) — only knobs *proven* live-reloadable
(today just `VIZ_HEALTH_REFRESH_SEC`, `MCP_AUTH_TOKEN`, both re-read per-iteration/
per-request) get `restart_required=False`.

---

## Backend API

All endpoints live under the existing **`/admin/` auth prefix** in
`yadgar/server/http.py` (Starlette via FastMCP, `@mcp_server.custom_route`). They
inherit `BearerAuthMiddleware` — Bearer `YADGAR_MCP_AUTH_TOKEN`, enforced when
`YADGAR_REQUIRE_AUTH=1`, token re-read per request. **No new auth mechanism.**

### `GET /admin/config` — extend the existing endpoint
Add the four missing fields to each row from `build_config_table()`:
```jsonc
{
  "config": [
    {
      "name": "fpa_threshold",
      "value": "0.25",
      "source": "yaml",            // env | yaml | default
      "kind": "float",             // int | float | bool | string
      "description": "Floor for fixed-point-attractor enrichment exemption",
      "category": "enrichment",    // mapped from FIELD_META.section
      "section": "retrieval_fusion",
      "restart_required": true,
      "destructive": false,
      "locked": false,             // true when source==env (yaml write can't override)
      "enum_choices": null         // or ["a","b","c"] for fixed-set strings
    }
  ],
  "daemon_version": "5.81.0",
  "pending_restart": false,        // see restart signaling
  "generated_at": "2026-06-23T…"
}
```
Backwards-compatible: existing callers reading `{name,value,source,kind}` keep working.

### `PATCH /admin/config/<key>` — new, write path
- Body: `{"value": <raw string or typed>}`.
- **MUST go through the sanctioned writer** — the exact path `yadgar config set`
  uses: `coerce_value(key, raw)` (infers type from `Settings.model_fields[KEY].annotation`,
  handles bool/int/float/str/list) → `ruamel.yaml` load/mutate/dump of
  `~/.config/yadgar/config.yaml` → `chmod 0o600`. **Never hand-write yaml bypassing
  `coerce_value`.** Factor `cmd_config_set`'s core into a reusable
  `set_config_value(key, raw) -> coerced` so CLI and API share one writer (and one
  validation path).
- **Validation / refusals (HTTP 4xx):**
  - key not in `Settings.model_fields` → 404.
  - `source == env` (locked) → **409 Conflict**, body explains env precedence; the
    yaml write would be silently shadowed, so refuse it.
  - type coercion failure → 422 with the coercion error.
  - `destructive` knob without the `armed` flag → **428 Precondition Required**
    (see destructive flow).
- Response: the updated row (same shape as GET) + `restart_required` echoed so the UI
  can move the change into the restart-needed bucket.
- **I25:** the writer only ever sets keys that exist in `Settings`; because every such
  key is already in FIELD_META **and** the registry (or the env-only allowlist), a
  successful write never violates the three-way-sync invariant. The PATCH path does
  not add knobs, so it cannot drift the invariant.

### Restart signaling + health
Config is read at **daemon startup**; most knobs need a restart to take effect
(live-reload is explicitly out of scope per `VIZ_CONFIG.md`). So:
- `GET /admin/health` (extend if exists, else add): `{daemon_version, started_at,
  uptime_s, config_loaded_at}`. The UI compares `config_loaded_at` against the
  newest yaml mtime to compute `pending_restart` (a write after load → restart
  pending). This is the honest signal — no need to track per-key dirty state server
  side beyond the mtime/load-time comparison.
- `POST /admin/restart` (P3, **guarded**): triggers a daemon restart (graceful:
  re-exec or signal the supervisor). **This drops the MCP connection** — the response
  must be sent *before* the restart fires, and the UI must warn loudly (the panel's
  own API calls will fail mid-restart; it polls `/admin/health` to detect the new
  `started_at`). Behind the same Bearer auth; rate-limited; audit-logged.
- **Audit log:** every PATCH and every restart appends to an audit sink
  (`{ts, actor=token-fingerprint, key, old, new, source}`). Destructive writes and
  restarts are always logged. This is the accountability backstop for a UI that can
  disable the secret-gate or enable purge.

---

## Frontend design spec

Implemented as a route/panel in the existing viz frontend. Reference mockup:
**`docs/plans/viz-config-control-panel.mockup.html`** (self-contained, open in a
browser). Aesthetic = **NEURAL CONSOLE**: scientific instrument panel for a memory
organism. Deep-ink base, amber→coral "heat" accent (heat is Yadgar's core concept),
teal "synapse" for active/info, alarm-red for armed/destructive. Display serif
(Fraunces) for headers + mono (IBM Plex Mono) for all data/values.

**Layout — 3 columns:**
1. **Left category rail** — the 15 CAPABILITY categories, each with a live count of
   knobs + pending-change badge; soft glow behind the active category. Search box at
   top filters across all 299.
2. **Center setting cards** — grouped by category (section as sub-group label),
   staggered reveal on load. Per-card components:
   - **bool → tactile toggle** with on-glow.
   - **int/float threshold → slider** with the live value rendered in mono
     (FPA `0.25`, quality floor, heat `decay_factor 0.9995`); decay/heat sliders may
     pulse subtly (heat motif).
   - **enum/string → select or text field.**
   - **source badge:** `DEFAULT` (ghost) / `YAML` (teal) / `ENV` (locked, lock icon,
     card non-editable).
   - **`⟲ restart required` pill** on knobs with `restart_required=true`.
   - **destructive card** (e.g. `COLD_MEMORY_PURGE_ENABLED`): red border,
     `⚠ deletes data`, **two-step armed confirm** — first click arms (10s window),
     second commits; never a one-click toggle.
3. **Right commit tray** — pending changes as old→new diffs, an **Apply** button
   (fires the PATCH calls), and a **Restart daemon** button that *appears only when a
   pending change is `restart_required`*, carrying the **"restarting drops the MCP
   connection"** warning.

**Header status line:** daemon version, pending-change count, `pending_restart`
indicator. **Atmosphere:** subtle grain + grid texture, soft glow behind active
category, staggered card reveal.

---

## Phases

- **P1 — read-only viewer.** Extend `GET /admin/config` with description + category
  (section→category map) + locked flag. Build the 3-col read-only panel: rail, cards,
  search/filter/group, source badges. No writes. Ships value immediately (finally a
  *legible* view of all 299 knobs).
- **P2 — editable.** `set_config_value()` refactor + `PATCH /admin/config/<key>`.
  Inline edit for bool/int/float/enum/string with type validation; commit tray with
  Apply; 409 on env-locked. Still no restart, no destructive.
- **P3 — restart integration + badges.** Add `restart_required` metadata (Option A,
  with I25 test update) + `destructive` field; `/admin/health` config-loaded-at
  signal; `POST /admin/restart` guarded; restart pill + commit-tray Restart button +
  MCP-drop warning + health polling for the new `started_at`.
- **P4 — destructive-confirm + audit.** Two-step armed confirm for destructive knobs
  (428 without `armed`); audit log of every PATCH + restart; rate-limit on restart.

---

## Risks

- **Security — this UI is powerful.** It can disable the secret-gate or enable
  `COLD_MEMORY_PURGE_ENABLED`. Mitigations: same Bearer auth as the rest of `/admin/`
  (enforce `YADGAR_REQUIRE_AUTH=1` in any non-localhost deploy); armed two-step
  confirm on destructive knobs; **audit log** of every write + restart with actor
  token fingerprint. Do not ship P2 (writes) to a network-exposed instance without
  auth on.
- **Env-vs-yaml precedence confusion.** A user edits a knob in the UI, the write
  succeeds to yaml, but an env var still wins → the value "doesn't change." Mitigation:
  env-sourced knobs are **locked** (read-only, lock badge), and PATCH returns **409**
  rather than silently writing a shadowed yaml key.
- **Restart drops the MCP connection.** The panel's own transport dies mid-restart.
  Mitigation: send the restart response *before* re-exec; poll `/admin/health` for a
  new `started_at`; loud pre-restart warning; never auto-restart.
- **I25 three-way-sync on write.** Adding `restart_required`/`destructive` to
  `ConfigEntry` (Option A) changes the registry shape → must update
  `test_config_three_way_sync.py` and keep the conservative `restart_required=True`
  default so un-annotated knobs fail safe. The PATCH writer itself can't violate I25
  (it only sets pre-existing `Settings` keys).
- **299-setting scale.** Raw is unusable. Mitigation: category rail + search +
  section sub-grouping + collapse-by-default for advanced categories; lazy-render
  cards.
- **section→category map drift.** New `FIELD_META.section` with no mapping → silent
  fallback to `config`. Mitigation: a test asserting every section in `FIELD_META`
  has an entry in the section→category map.

---

## References
- `yadgar/graph_api.py`, `yadgar/server/http.py` — viz REST backend, route patterns.
- `yadgar/server/admin_config.py` — existing `/admin/config` (`build_config_table()`).
- `yadgar/config_registry.py` — `_REGISTRY` (~175 `ConfigEntry` rows), `ConfigEntry`, `build_config_table()`.
- `yadgar/config_yaml.py` — `FIELD_META` (desc/section), `cmd_config_set/get/list`,
  `coerce_value`, the sanctioned yaml writer.
- `yadgar/tests/config_env_only_allowlist.txt` — env-only knobs (locked in UI).
- `yadgar/tests/test_config_three_way_sync.py` — I25 enforcement.
- `docs/ARCHITECTURE_INVARIANTS.md` (I25), `docs/CAPABILITY_REGISTRY.md` (categories),
  `docs/VIZ_CONFIG.md` (restart reality, live-reload out of scope),
  `docs/BEHAVIOR_CONTRACT.md`.
