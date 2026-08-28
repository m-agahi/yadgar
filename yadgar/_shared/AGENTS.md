# AGENTS.md — `yadgar/_shared/` (shared layer)

Rules for agents editing this layer. The layer README explains what lives
here; this file is the placement law. Full rationale: the layer-boundary
train plan (T2, 2026-07-09) + ADR-0078 / ADR-0084 / ADR-0056.

## Placement laws (violations get bounced in review)

1. **Dual-import law.** A module belongs in `_shared/` ONLY if it is directly
   or indirectly imported by BOTH `core/` and `backend/`. Single-layer
   consumers → move the module to that layer.
2. **Semantic law (wins on conflict).** Anything needing COMPUTE
   (numpy/matrix/scoring), heavy transformation, or stateless-work-over-DB-data
   belongs in `backend/` even if all current importers are core.
3. **No lone files (ADR-0084).** Every module is a package directory. Never
   add a new flat `.py` at this layer root. Do **not** read the survivors as
   sweepable back-compat, which is what this line used to say: of the five flat
   modules here, exactly ONE is a PEP-562 shim (`cognitive_map.py`, forwarding
   to `backend.restoration` by lazy string-target import so no static
   `_shared → backend` edge exists). `errors.py`, `refusal.py`, `trace_mesh.py`
   and `version_compat.py` are real modules with live importers in both layers.
   Verified 2026-08-28 — `grep -l __getattr__ yadgar/_shared/*.py` returns
   `cognitive_map.py` alone.
4. **Contract/impl split.** If the other layer needs only a
   dataclass/Protocol, keep the contract here (`contracts/`, `wiki/contract`,
   `restoration/contract`) and move the impl to its layer.

## Import direction

- `_shared` must import NOTHING from `core/` or `backend/`. Enforced by
  import-linter contract 1 (pre-commit `lint-imports`, hard-fail).
- The ONLY sanctioned exceptions are the composition-root edges in
  `runtime/lifecycle.py` (`-> backend.ml_client`, `-> backend.cache`),
  permanently waived per ADR-0056. Do not add more; do not "fix" those.
- Cross-layer back-compat shims here forward via **string-target importlib**
  (lazy) so no static edge exists. Same-layer re-exports may do the same for
  uniformity.

## Don't

- Don't add DB write calls outside `storage/` — core-side write paths forward
  through the file-queue seam or backend endpoints (ADR-0078).
- Don't import `yadgar.core.*` / `yadgar.backend.*` — see above.
- Don't grow `retrieval/` — the compute already sank to `backend/retrieval/`;
  what is left here is `profiles.py` and its package shell. New retrieval
  compute goes to backend.
- Don't hand-write a new PEP-562 shim variant — copy `cognitive_map.py`
  verbatim. (This line used to name `config_yaml.py`, which is not at this
  layer root at all — it lives at `_shared/config/config_yaml.py` and is not a
  shim.)

## Forward seams (where cross-layer work goes instead)

- Writes: `file_queue/` (queue → backend drainer) — the sanctioned path.
- Compute: backend HTTP endpoints (`/embed`, `/rerank`, `/recall`,
  `/restore`, `/consolidate`).
- Selection/injection of concrete backend objects: `runtime/lifecycle.py`
  composition root ONLY.
