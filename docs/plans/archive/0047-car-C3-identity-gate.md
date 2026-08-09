# Car C3 — Redesign + reimplement the identity gate (D21)

> Parent plan: docs/plans/task-table-refactor-2026-07-29.md (task 0047, §7 + §16)
> Status: shipped
> Depends on: —
> Lifecycle: ADR-0081/0082 — archive this doc as the first commit of the completing branch; mark partial scope in the status header if shipped incomplete.

## 1. Scope

Car C3 is D21: **"Redesign and reimplement the identity gate; `gate_mode="identity"`
for all three types.** The "identity gate" is the wiki-write duplicate-detection
gate's `gate_mode="identity"` dispatch path — the slug-based alternative to the
content-embedding+KNN similarity gate. It was removed with `repo_wiki`'s
decommission (#33/ADR-0162); §1.4 confirms it is **dead code** today — no
`page_type` sets `gate_mode="identity"`, so every `wiki_add` runs the similarity
gate. D21 is a **build, not a flip**: the identity gate must be reimplemented,
then `gate_mode="identity"` set for the three canonical/deterministic-slug page
types (`adr`, `task_list`, and the agent-prompt library types). Rationale
(verbatim from D21): "Deterministic slugs, legitimately self-similar content."
ADR/task-list/agent-prompt pages share structural prose — the similarity gate
false-positives on them, which is why every sanctioned canonical writer currently
bypasses it (`force=True` / `replace_slug`). The identity gate uses the slug as
identity (a duplicate = same slug, not similar content), so deterministic-slug
types pass without a bypass flag. **D21 deletes `adr_add`'s `force=True`.**

**SCOPE CLARIFICATION — D21 does NOT change the gate from branch-scoped to
`project_id`-scoped.** The D21 decision-table row (§2.5, line 252) says nothing
about scoping. Branch scoping was already removed by ADR-0215 (shipped —
`find_similar_wiki_pages` is "NOT branch-aware", `store.py:1048-1049`). The
directory→`project_id` migration is Car L (§16.11). D21 is about the gate
**MODE** (identity vs similarity), not the gate **SCOPE**. The identity gate
inherits whatever scope the similarity gate uses (today: directory-scoped via
`is_directory_eligible`, `store.py:1051-1057`; post-Car-L: `project_id`-scoped).

## 2. Touched files

| file | change | verified |
|------|--------|----------|
| `yadgar/_shared/wiki/policy.py:98-104` | `DEFAULT_POLICY` stays `gate_mode="similarity"` (the default for free-form `wiki_add` pages — `reference`/`decision`/`note` types that SHOULD run the similarity gate). No edit. | `policy.py:98-104` read |
| `yadgar/_shared/wiki/policy.py:107-113` | `_AGENT_LIBRARY_POLICY`: change `gate_mode="similarity"` → `gate_mode="identity"`. Covers `agent_pattern`, `agent_discipline`, `agent_index`, and legacy `agent_prompt` (all four keyed to this one policy at `:126-133`). | `policy.py:107-113` read; `gate_mode` field at `:73` |
| `yadgar/_shared/wiki/policy.py:123-134` | Add TWO new `POLICY_BY_TYPE` entries for `adr` and `task_list`, each a `WikiPolicy(gate_mode="identity", recall_disposition=..., dir_scope="strict", merge=..., storage_scope=...)`. [VERIFY: the exact `recall_disposition`/`merge`/`storage_scope` values for `adr` and `task_list` — today both fall through to `DEFAULT_POLICY` (`recall_disposition="include"`, `merge="allow"`, `storage_scope="project"`); the new entries MUST preserve those values so the identity-gate change does not silently alter recall visibility or merge semantics. Decide at build time whether `adr` gets `merge="never"` (D26 says `adr`/`adr_superseded` → locked, but `merge` here is the wiki-merge policy, not the mutability lock — [VERIFY: whether D26's `locked` maps to `merge="never"` or is orthogonal]).] | `POLICY_BY_TYPE` at `:123-134` read; only agent types present today, no `adr`/`task_list` entries |
| `yadgar/_shared/wiki/policy.py:73` | `gate_mode: str` field — docstring `:74` says `"""``"similarity"`` or ``"identity"``."""`. Field already exists; no schema change. | `policy.py:73-74` read |
| `yadgar/backend/queue_drainer/dlq.py:202-237` | `_sim_gate_for_drainer`: re-introduce the policy dispatch that the dead comment at `:232-236` describes. After the bypass checks (`:219-224`) and before falling through to `_similarity_gate_for_drainer`, read `get_policy(payload.get("page_type")).gate_mode`; if `"identity"` → call the NEW `_identity_gate_for_drainer` (below); else fall through to similarity. | `dlq.py:202-237` read; current code always calls `_similarity_gate_for_drainer` at `:237` with no policy read |
| `yadgar/backend/queue_drainer/dlq.py:240-320` | `_similarity_gate_for_drainer`: no change (the similarity path for non-identity types). Reference only. | `dlq.py:240-320` read |
| `yadgar/backend/queue_drainer/dlq.py` (NEW method) | NEW `_identity_gate_for_drainer(self, payload) -> dict | None`: the identity gate. Slug-based identity — a page is a "duplicate" iff another page with the SAME slug already exists. For deterministic-slug types this is a pass-through (slug is unique by construction; a re-write of the same slug is an update, not a duplicate). Returns `None` (pass) in the normal case. [VERIFY: whether the identity gate should reject when an explicit-slug `wiki_add(upsert=False)` collides with an existing slug — that is already handled at `WikiStore.add` → `slug_exists` (`__init__.py:365-374,419`) and routed to DLQ from `_apply_pending` (`__init__.py:415-423`), so the identity gate likely does NOT duplicate that check; confirm at build time.] | `dlq.py` read; `slug_exists` path at `__init__.py:365-374,415-423` confirmed |
| `yadgar/backend/queue_drainer/__init__.py:594` | Reference only — the drain-loop call site `self._sim_gate_for_drainer(data.get("payload", {}))`. No edit (the dispatch change is inside `_sim_gate_for_drainer`). | `__init__.py:593-594` read |
| `yadgar/_shared/wiki/store.py:1030-1109` | Reference only — `find_similar_wiki_pages` (the similarity gate's embed+KNN candidate search). The identity gate does NOT call this. ADR-0215 scope confirmed at `:1048-1049` ("NOT branch-aware"). | `store.py:1030-1109` read |
| `yadgar/core/server/tools/adr_render.py:160` | DELETE `"force": True` from `_canonical_adr_payload`'s return dict (`:150-164`). Once `adr` page_type has `gate_mode="identity"`, canonical ADR pages pass the identity gate without bypassing it. | `adr_render.py:134-164` read; `"force": True` at `:160` confirmed |
| `yadgar/core/server/tools/adr_render.py:146` | UPDATE the docstring sentence `"``force=True`` bypasses the drainer sim gate (canonical ADR/index pages are legitimately near-duplicate)"` — replace with the identity-gate rationale. | `adr_render.py:143-148` read |
| `yadgar/core/server/tools/wiki.py:73-126` | `_wiki_write_canonical`: UPDATE docstrings at `:102-103` and `:117` that say "Sanctioned callers must pass `force=True` in the payload (canonical ADR/task-list pages are legitimately near-duplicate and must bypass the drainer sim gate)" — the ADR path no longer sets `force=True`. The `task_list` path uses `replace_slug` (`:205`) and is unaffected. [VERIFY: whether the `_internal=True` token (`:114`) still needs to be set — it is stripped by the drainer (`dlq.py:194`) and marks sanctioned server-side writes; it is NOT the gate bypass, so it stays.] | `wiki.py:73-126` read; `force=True` docstring at `:102-103,117`; `_internal=True` at `:114`; `task_list` `replace_slug` at `:205` |
| `yadgar/core/server/tools/wiki.py:29` | Reference only — `CANONICAL_PAGE_TYPES = frozenset({"task_list", "adr"})`. No edit (the identity gate is a policy dispatch, not a canonical-type change). | `wiki.py:29` read |
| `yadgar/core/server/tools/wiki.py:797-840` | Reference only — `wiki_check_duplicate` MCP tool (dry-run similarity check). The identity gate change does not affect this tool (it calls `find_similar_wiki_pages` directly, bypassing the policy dispatch). [VERIFY: whether `wiki_check_duplicate` should also respect `gate_mode="identity"` — for identity-gated types it would return an empty candidate list since the identity gate does not use embedding similarity. Decide at build time; likely out of scope for C3.] | `wiki.py:797-840` read |
| `yadgar/backend/admin_exec/wiki.py:416-454` | Reference only — backend `agent_prompt_save` calls `wiki.add()` directly (`:443`), NOT through the file-queue drainer. So agent-prompt writes BYPASS the drainer gate entirely today. The `_AGENT_LIBRARY_POLICY` `gate_mode="identity"` change is policy-consistent and covers any future drainer path, but does not alter the current admin-exec write path. No edit. | `admin_exec/wiki.py:416-454` read; `wiki.add()` at `:443` |
| `yadgar/core/server/tools/agent_prompts.py:111,171` | Reference only — core `agent_prompt_save` forwards to backend `/admin` (`:171` `_forward_admin`). No edit. | `agent_prompts.py:111,155-192` read |
| `server.json:10,11` | bump `backend_version` (5.71.0 today) — Car C3 touches `yadgar/backend/queue_drainer/dlq.py` (new gate method + dispatch). Mechanism: `scripts/check_backend_bump.py:44,51`, `BACKEND_BUILD_DIRS=("backend",)`. | `server.json:10-11` read; `check_backend_bump.py:44,51` read |
| `pyproject.toml:7` | bump core `version` (5.181.0 today) — Car C3 touches core (`adr_render.py`, `wiki.py`). `check_version_bump.py` triggers when any `yadgar/**` file changes and the version still matches the latest tag. | `pyproject.toml:7` read; `check_version_bump.py:95-100` read |

## 3. Functions / symbols

**MODIFY — `_sim_gate_for_drainer` dispatch** (`yadgar/backend/queue_drainer/dlq.py:202`):
```python
def _sim_gate_for_drainer(self, payload: dict) -> dict | None:
    # Bypass: force, replace_slug, append  (unchanged, :219-224)
    if payload.get("force"):
        return None
    if payload.get("replace_slug") is not None:
        return None
    if payload.get("append"):
        return None

    # D21: policy dispatch — identity vs similarity.
    from yadgar._shared.wiki.policy import get_policy  # noqa: PLC0415
    page_type = payload.get("page_type")
    gate_mode = get_policy(page_type).gate_mode
    if gate_mode == "identity":
        return self._identity_gate_for_drainer(payload)
    return self._similarity_gate_for_drainer(payload)
```

**NEW — `_identity_gate_for_drainer`** (`yadgar/backend/queue_drainer/dlq.py`):
```python
@observe(tier="stage", metric="drainer.dlq.identity_gate_for_drainer")
def _identity_gate_for_drainer(self, payload: dict) -> dict | None:
    """D21 identity gate: slug-based identity, no content-similarity check.

    For page_types with deterministic slugs (adr, task_list, agent_prompt
    library), a page's identity IS its slug — content similarity is
    irrelevant (two ADR pages are structurally near-identical by design).
    The gate is a pass-through: a re-write of the same slug is an update,
    not a duplicate. The upsert=False slug-collision case is already
    enforced at WikiStore.add → slug_exists (__init__.py:365-374,419).
    """
    return None
```
[VERIFY: whether the identity gate needs ANY active check beyond pass-through.
The old identity gate (pre-#33) ran `validate_repo_wiki_page` against
`repo_wiki_schema` — that validator is gone with repo_wiki. D21 says
"redesign + reimplement", not "restore". A pure pass-through may be the
correct redesign for deterministic-slug types, but confirm at build time
whether the gate should validate slug format against the page_type's
expected slug pattern (e.g. `^yadgar-adr-[0-9]+$` for `adr`).]

**MODIFY — `_canonical_adr_payload`** (`yadgar/core/server/tools/adr_render.py:150`):
```python
# DELETE the "force": True line (:160). The dict becomes:
return {
    "wiki_schema_version": 2,
    "slug": slug,
    "title": slug,
    "content": content,
    "category": category,
    "tags": tags,
    "source_memory_ids": None,
    "confidence": "high",
    "append": False,
    # "force" removed — adr page_type is gate_mode="identity" (D21).
    "replace_slug": replace_slug,
    "directory_context": directory,
    "page_type": "adr",
}
```

**MODIFY — `POLICY_BY_TYPE`** (`yadgar/_shared/wiki/policy.py:123`):
```python
_ADR_POLICY = WikiPolicy(
    gate_mode="identity",
    recall_disposition="include",   # [VERIFY: preserve DEFAULT_POLICY value]
    dir_scope="strict",
    merge="allow",                  # [VERIFY: D26 locked vs merge=never]
    storage_scope="project",
)
_TASK_LIST_POLICY = WikiPolicy(
    gate_mode="identity",
    recall_disposition="include",   # [VERIFY: preserve DEFAULT_POLICY value]
    dir_scope="strict",
    merge="allow",
    storage_scope="project",
)

POLICY_BY_TYPE: dict[str, WikiPolicy] = {
    PAGE_TYPE_AGENT_PROMPT_LEGACY: _AGENT_LIBRARY_POLICY,  # now identity
    PAGE_TYPE_AGENT_PATTERN: _AGENT_LIBRARY_POLICY,
    PAGE_TYPE_AGENT_DISCIPLINE: _AGENT_LIBRARY_POLICY,
    PAGE_TYPE_AGENT_INDEX: _AGENT_LIBRARY_POLICY,
    "adr": _ADR_POLICY,           # NEW
    "task_list": _TASK_LIST_POLICY,  # NEW
}
```
[VERIFY: `PAGE_TYPE_ADR` / `PAGE_TYPE_TASK_LIST` constants — none exist in
`wiki_meta.py` (only agent-type constants at `:41,46,53,57`); `adr` and
`task_list` are string literals in `CANONICAL_PAGE_TYPES` (`wiki.py:29`).
Use string literals `"adr"` / `"task_list"` in the registry to match.]

## 4. Build steps (TDD)

1. **RED** — `tests/backend/test_wiki_gate_dir_scope_and_identity.py` (or a new
   `test_wiki_identity_gate.py`): assert that a `wiki_add` payload with
   `page_type="adr"` and content near-identical to an existing `adr` page PASSES
   the drainer gate (no `duplicate_detected` rejection), without `force=True`.
   Today this fails — `adr` falls through to `DEFAULT_POLICY` (`gate_mode="similarity"`),
   the similarity gate fires, and the write is rejected.

2. **RED** — assert the same for `page_type="task_list"` (near-identical task-list
   content passes without `replace_slug` or `force=True`).

3. **RED** — assert `_canonical_adr_payload(...)` returns a dict with NO `"force"`
   key (or `"force": False`). Today it returns `"force": True` at `adr_render.py:160`.

4. **GREEN** — add `_ADR_POLICY` / `_TASK_LIST_POLICY` with `gate_mode="identity"`
   to `POLICY_BY_TYPE` (`policy.py:123`). Re-introduce the policy dispatch in
   `_sim_gate_for_drainer` (`dlq.py:202`) reading `get_policy(page_type).gate_mode`.
   Add `_identity_gate_for_drainer` (pass-through). Delete `"force": True` from
   `_canonical_adr_payload` (`adr_render.py:160`).

5. **GREEN** — verify the existing similarity-gate tests still pass for
   `page_type=None` / `page_type="reference"` (free-form pages still run the
   similarity gate via `DEFAULT_POLICY`).

6. **REFACTOR** — the dead comment at `dlq.py:232-236` ("no page_type sets
   `gate_mode='identity'` any more") is now FALSE — update it to describe the
   D21 identity-gate dispatch. Update the `_wiki_write_canonical` docstring
   (`wiki.py:102-103,117`) and `_canonical_adr_payload` docstring
   (`adr_render.py:143-148`) to remove the `force=True` rationale.

7. **RED** — regression: assert a free-form `wiki_add(page_type="reference")`
   with near-duplicate content is STILL rejected by the similarity gate
   (proves the identity gate did not accidentally swallow the default path).

## 5. Acceptance gates

- [ ] `page_type="adr"` wiki_add with near-duplicate content passes the drainer
      gate WITHOUT `force=True` (identity gate, not similarity).
- [ ] `page_type="task_list"` wiki_add with near-duplicate content passes the
      drainer gate WITHOUT `force=True` / `replace_slug`.
- [ ] `page_type="reference"` (and `page_type=None`) wiki_add with near-duplicate
      content is STILL rejected by the similarity gate (default path unchanged).
- [ ] `_canonical_adr_payload` no longer sets `"force": True`.
- [ ] `adr_add` end-to-end writes a per-ADR page successfully (no gate rejection)
      — the existing `adr_add` tests pass without the `force=True` bypass.
- [ ] `get_policy("adr").gate_mode == "identity"` and
      `get_policy("task_list").gate_mode == "identity"` and
      `get_policy("agent_pattern").gate_mode == "identity"`.
- [ ] `get_policy(None).gate_mode == "similarity"` and
      `get_policy("reference").gate_mode == "similarity"` (default unchanged).
- [ ] core/backend version bumped per WORKFLOW RULE — `backend_version` in
      `server.json:11` (Car C3 touches `yadgar/backend/queue_drainer/dlq.py`);
      core `version` in `pyproject.toml:7` (touches `adr_render.py`, `wiki.py`).
      [VERIFY exact bump mechanism: `scripts/check_backend_bump.py:44,51` with
      `BACKEND_BUILD_DIRS=("backend",)` for backend; `scripts/check_version_bump.py:95-100`
      triggers on any `yadgar/**` change when version matches latest tag.]
- [ ] pre-commit green (ruff, import-linter I32/I33, `check_versions`).
- [ ] tests pass — including the existing `test_wiki_sim_gate_drainer.py` and
      `test_wiki_gate_dir_scope_and_identity.py` (the latter's docstring at
      `:8-12` says "no page_type sets `gate_mode='identity'`" — update it).

## 6. Sequencing

Car C3 has **no hard dependencies** (§7 row: depends on —). The gate dispatch
reads `get_policy(page_type)` which is a pure function over the policy registry
(no DB, no ledger). The identity gate is self-contained in the drainer.

**Cars that wait on C3:** none directly. The §7 ordering says "C gates D and F"
— Car C (C1+C2+C3) gates Cars D and F. Car C3's contribution to that gate:
once `adr` has `gate_mode="identity"` and `adr_add`'s `force=True` is deleted,
the ADR canonical write path is clean for Car F (ADR tools re-pointed) and
Car G (ADR seed + retype) to build on. Car G's retype mutator adds
`adr_superseded` to `CANONICAL_PAGE_TYPES` — [VERIFY: whether `adr_superseded`
should also get a `POLICY_BY_TYPE` entry with `gate_mode="identity"`; likely
yes, but that is Car G's call, not C3's].

**Interaction with Car L (directory→project_id backfill):** the identity gate
does NOT read `directory_context` or `project_id` — it is slug-based. So Car L's
scope migration does not change C3's behavior. The two are independent.

## 7. ADRs / decisions

- **D21** (§2.5, `:252`) → Redesign and reimplement the identity gate;
  `gate_mode="identity"` for all three types. Currently dead code (§1.4) —
  this is a build, not a flip. Deletes `adr_add`'s `force=True`.
- **ADR-0162** (#33, referenced at `dlq.py:234-235`, `policy.py:137-139`) →
  repo_wiki decommission removed the old identity gate + `repo_wiki_schema`
  validator. D21 rebuilds it without repo_wiki.
- **ADR-0215** (shipped, `store.py:1048-1049`) → removed branch scoping from
  `find_similar_wiki_pages`. NOT a D21 concern — the identity gate inherits
  the similarity gate's scope (directory-scoped today, `project_id`-scoped
  post-Car-L).
- **D26** (§2.5, `:257`) → `adr`/`adr_superseded` → locked mutability. [VERIFY:
  whether D26's `locked` maps to `merge="never"` in the new `_ADR_POLICY` or
  is orthogonal (mutability is enforced at `storage/wiki.py:215`, not at the
  policy `merge` field).]
- **ADR-0209** (referenced at `policy.py:114-121`) → split `agent_prompt` into
  `agent_pattern` + `agent_discipline` + `agent_index`; all three share
  `_AGENT_LIBRARY_POLICY`, so one `gate_mode` change covers all of them.

## 8. Out of scope

- **The similarity gate itself** (`_similarity_gate_for_drainer`,
  `find_similar_wiki_pages`) — unchanged. C3 adds an identity path, does not
  touch the similarity path.
- **The gate's scope** (branch/directory/`project_id`) — NOT D21. Branch
  removal is ADR-0215 (shipped); directory→`project_id` is Car L. C3 inherits
  whatever scope exists.
- **`wiki_check_duplicate` MCP tool** (`wiki.py:797`) — calls
  `find_similar_wiki_pages` directly, not the policy dispatch. [VERIFY: whether
  it should respect `gate_mode="identity"` — likely a separate car.]
- **Car G's `adr_superseded` type** — Car G adds it to
  `CANONICAL_PAGE_TYPES`; whether it gets a `POLICY_BY_TYPE` identity entry is
  Car G's decision.
- **The `upsert=False` slug-collision check** (`__init__.py:365-374,415-423`) —
  already enforced at `WikiStore.add` → `slug_exists`. The identity gate does
  not duplicate it.
- **`task_list`'s `replace_slug` bypass** (`wiki.py:205`) — `wiki_write_task_list`
  uses `replace_slug=slug` (not `force=True`) to skip the gate. Once
  `task_list` has `gate_mode="identity"`, the `replace_slug` bypass becomes
  redundant for the GATE purpose, but `replace_slug` also means "overwrite the
  existing page in place" (upsert semantics), which is independently needed.
  Do NOT delete `replace_slug` from `wiki_write_task_list` — it serves the
  upsert contract, not just the gate bypass.

## 9. Risks / open questions

- **[VERIFY: identity gate = pure pass-through?]** D21 says "redesign +
  reimplement", not "restore". The old identity gate ran
  `validate_repo_wiki_page` against `repo_wiki_schema` — that validator is
  gone. A pure pass-through (return None) is the simplest correct redesign
  for deterministic-slug types: the slug IS the identity, and slug collisions
  are already handled by `WikiStore.add` → `slug_exists`. But confirm at
  build time whether the gate should additionally validate the slug format
  against the page_type's expected pattern (e.g. `^yadgar-adr-[0-9]+$`).
  Risk of NOT validating: a malformed slug on a `page_type="adr"` write passes
  silently; but `adr_add` constructs the slug server-side (`adr_render.py:150`),
  so a model cannot inject a malformed slug on the canonical path. On the
  general `wiki_add(page_type="adr")` path, a model CAN supply an arbitrary
  slug — [VERIFY: whether that is a real risk or is already blocked by
  `CANONICAL_PAGE_TYPES` / `_wiki_write_canonical`'s page_type allowlist].

- **[VERIFY: `_ADR_POLICY` `merge` field]** — D26 says `adr` → locked. The
  `WikiPolicy.merge` field (`:82-83`) is `"allow"` or `"never"`. Whether
  `locked` maps to `merge="never"` or is orthogonal (enforced at
  `storage/wiki.py:215`, not the policy `merge` field) must be confirmed at
  build time. Setting `merge="never"` on `_ADR_POLICY` could affect wiki-merge
  behavior beyond the gate dispatch.

- **[VERIFY: `recall_disposition` for `adr`/`task_list`]** — today both fall
  through to `DEFAULT_POLICY` (`recall_disposition="include"`). The new
  `POLICY_BY_TYPE` entries MUST preserve `"include"` so the identity-gate
  change does not silently alter recall visibility. D22 (§2.5, `:253`) makes
  `recall_disposition` status-driven for `adr` (`accepted`/`open` → include,
  `superseded` → exclude) — that is Car G's retype + D22, NOT C3. C3's
  `_ADR_POLICY` should keep `recall_disposition="include"` and let Car G's
  status-driven disposition layer handle exclusion.

- **[VERIFY: agent_prompt writes bypass the drainer entirely]** —
  `agent_prompt_save` → backend admin exec → `wiki.add()` directly
  (`admin_exec/wiki.py:443`), NOT through the file-queue drainer. So setting
  `_AGENT_LIBRARY_POLICY.gate_mode="identity"` has no effect on the current
  agent-prompt write path (it never hits the drainer gate). The change is
  policy-consistent and covers any future drainer path, but is not
  behavior-changing for agent_prompt today. Confirm this is the intent.

- **[VERIFY: `adr_add` tests that rely on `force=True`]** — existing tests
  (`tests/core/test_input_robustness.py:136,150,164`,
  `tests/core/test_project_brief_catalog_v5530.py:62,273,301`) pass
  `force=True` on `wiki_add` calls. Deleting `force=True` from
  `_canonical_adr_payload` does NOT remove the `force` parameter from the
  `wiki_add` MCP tool (`wiki.py:304`) — it only removes it from the ADR
  canonical payload. But any test that asserts `_canonical_adr_payload`
  returns `"force": True` will break. Audit the test suite at build time.
