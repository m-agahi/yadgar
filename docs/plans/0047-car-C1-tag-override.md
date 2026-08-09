# Car C1 — tag-override matches the page type's own opt-in tag

> Parent plan: docs/plans/task-table-refactor-2026-07-29.md (task 0047, §7 + §16)
> Status: build-ready (spec extracted from audited master plan)
> Depends on: —
> Lifecycle: ADR-0081/0082 — archive this doc as the first commit of the completing branch; mark partial scope in the status header if shipped incomplete.

## 1. Scope

Narrow the recall tag-override so an excluded page survives search ONLY when the caller's
`opt_in_tags` include that page **type's** declared opt-in tag — not, as today, when any tag the
page happens to carry intersects the caller's tags. This is sub-decision **3a** of the three-part
retrieval/gate redesign (3a/3b/3c = C1/C2/C3): C1 tightens the opt-in, C2 implements `downweight`
(D22's third disposition), C3 reimagines the identity gate (D21).

Today (`yadgar/_shared/wiki/policy.py:186-190` `is_recall_visible`): an excluded page survives iff
`set(page.tags) ∩ set(opt_in_tags) ≠ ∅` — page-centric, any matching tag. §1.4 calls this out as
"the tag-override defeats exclusion … any tagged recall skips it." Task 0134 already narrowed the
PRE-0134 rule (which gated the whole filter on `if not self._tags`, so one unrelated tag disabled
exclusion for EVERY page) down to the per-page intersection. C1 narrows it once more: the unlock
signal is the **type's** opt-in tag, not the page's own tag set. This is the defense-in-depth step
before Car I + D24 kill the agent-prompt tag-override entirely (agent-prompt discovery becomes
`agent_prompt_list`/`agent_prompt_get`, and `agent_prompt` is excluded unconditionally).

After C1: `WikiPolicy` gains an `opt_in_tag: str | None` field (None = unconditional exclusion, no
tag unlocks). `is_recall_visible` becomes type-centric — it reads `page.page_type` and the caller's
`opt_in_tags`, and IGNORES `page.tags` for the exclusion gate (page tags still matter upstream for
the SQL pre-filter / HNSW ranking path, unaffected by this car). `agent_pattern`,
`agent_discipline`, and the legacy `agent_prompt` type declare `opt_in_tag="agent-prompt"` (the
documented `recall(tags=["agent-prompt"])` lookup). `agent_index` (the TOC) declares
`opt_in_tag=None` — unconditionally excluded, closing the §1.4 leak where the TOC surfaced on every
targeted prompt lookup.

## 2. Touched files

| file | change | verified |
|------|--------|----------|
| `yadgar/_shared/wiki/policy.py:63` | Add `opt_in_tag: str \| None = None` as a `WikiPolicy` field (appended after `storage_scope` with a default so existing 4–5-arg positional callers keep working per the class docstring at `:69`). Set per-type values in `POLICY_BY_TYPE` (`:123`). | `yadgar/_shared/wiki/policy.py:63` `@dataclass(frozen=True) class WikiPolicy`, `:85` `storage_scope: str = "project"` (current last field), `:69` "New fields MUST be appended with defaults" — confirmed via Read |
| `yadgar/_shared/wiki/policy.py:107` | `_AGENT_LIBRARY_POLICY` — add `opt_in_tag="agent-prompt"` so `agent_pattern`, `agent_discipline`, and legacy `agent_prompt` share the opt-in tag. | `:107` `_AGENT_LIBRARY_POLICY = WikiPolicy(...)`, `:123-128` the three types map to it — confirmed |
| `yadgar/_shared/wiki/policy.py:133` | `PAGE_TYPE_AGENT_INDEX` entry — split the TOC off the shared policy: give it its own `WikiPolicy(..., opt_in_tag=None)` (unconditional exclusion) OR keep the shared instance and override via a per-type opt-in-tag map. See §3 for the chosen shape. | `:133` `PAGE_TYPE_AGENT_INDEX: _AGENT_LIBRARY_POLICY` — confirmed |
| `yadgar/_shared/wiki/policy.py:163` | Rewrite `is_recall_visible` body (`:186-190`): excluded page survives iff `policy.opt_in_tag is not None and policy.opt_in_tag in set(opt_in_tags)`. Stop reading `page.tags` for the gate. | `:163` `def is_recall_visible(page, opt_in_tags)`, `:186-190` current intersection logic — confirmed |
| `yadgar/tests/backend/test_wiki_provider_policy_exclusion.py:110` | Revise `test_opt_in_is_tag_intersection` — the behavior it pins (a `tags=["yadgar"]` call unlocking an `agent_pattern` page carrying `yadgar`) is EXACTLY what C1 reverses. New assertion: the page is DROPPED under `tags=["yadgar"]` (not the type's opt-in tag) and SURVIVES under `tags=["agent-prompt"]`. | `:110` `def test_opt_in_is_tag_intersection`, `:119-121` fixture — confirmed |
| `yadgar/tests/backend/test_wiki_provider_policy_exclusion.py:129` | Revise `test_legacy_agent_prompt_type_still_excluded` — under C1 the legacy `agent_prompt` page SURVIVES `tags=["agent-prompt"]` (the type's opt-in tag is `"agent-prompt"`, and the caller passes it), regardless of the page's own `["agent-prompt-toc"]` tags. Rename to `test_legacy_agent_prompt_type_opted_in_via_type_tag` and assert survival; add a companion asserting bare recall still drops it. | `:129` `def test_legacy_agent_prompt_type_still_excluded`, `:132-136` — confirmed |
| `yadgar/tests/core/test_wiki_query_policy_exclusion.py:88` | Revise `test_opt_in_is_tag_intersection` — under C1 `tags=["quokka"]` no longer unlocks the two excluded pages (`quokka` is not any excluded type's opt-in tag). New assertion: only `plain-quokka-page` survives; the agent_pattern page survives only under `tags=["agent-prompt"]`. | `:88` `def test_opt_in_is_tag_intersection`, `:95-96` — confirmed |
| `yadgar/tests/backend/test_wiki_provider_policy_exclusion.py:61` | `test_toc_dropped` / `:82` `test_non_matching_excluded_page_still_dropped` — still pass under C1 (TOC `opt_in_tag=None` → always dropped). Add a NEW test asserting the TOC stays dropped even under `tags=["agent-prompt-toc"]` (its own tag) — unconditional exclusion. | `:61` `def test_toc_dropped`, `:82` `def test_non_matching_excluded_page_still_dropped` — confirmed |
| `yadgar/__init__.py:21` | Core version bump per WORKFLOW RULE (C1 touches `yadgar/_shared/**` → pyproject `version` bump, mirrored to `server.json` `version`). `BACKEND_VERSION` bump ONLY if `yadgar/backend/**` is touched — see §5. | `yadgar/__init__.py:21` `BACKEND_VERSION = "5.71.0"`, core `__version__` via `importlib.metadata` (`:4`) — confirmed; `scripts/check_backend_bump.py:44` `BACKEND_BUILD_DIRS = ("backend",)` — `_shared` does NOT trigger backend bump |

## 3. Functions / symbols

**New / modified:**

- `WikiPolicy` (`yadgar/_shared/wiki/policy.py:63`) — add field `opt_in_tag: str | None = None`
  (appended after `storage_scope` at `:85`, with default; the class docstring at `:69` already
  mandates "New fields MUST be appended with defaults so existing 4-arg positional callers keep
  working"). Semantics: `None` = unconditional exclusion (no tag unlocks this type); a string
  value = the one tag that unlocks it when present in the caller's `opt_in_tags`.

- `POLICY_BY_TYPE` (`yadgar/_shared/wiki/policy.py:123`) — set `opt_in_tag` per excluded type:
  - `PAGE_TYPE_AGENT_PATTERN` / `PAGE_TYPE_AGENT_DISCIPLINE` / `PAGE_TYPE_AGENT_PROMPT_LEGACY`
    (`:126-128`) → `opt_in_tag="agent-prompt"` (the documented `recall(tags=["agent-prompt"])`
    lookup, referenced at `core/server/tools/wiki.py:590` and `backend/admin_exec/wiki.py:61`).
  - `PAGE_TYPE_AGENT_INDEX` (`:133`) → `opt_in_tag=None` (unconditional — the TOC must never be
    recall-visible, §1.4; Car I later deletes the TOC page entirely). This requires splitting the
    TOC off `_AGENT_LIBRARY_POLICY`: either (a) give `agent_index` its own `WikiPolicy` instance
    with `opt_in_tag=None`, or (b) keep one shared `_AGENT_LIBRARY_POLICY` instance and add a
    separate `OPT_IN_TAG_BY_TYPE: dict[str, str | None]` map that `is_recall_visible` consults.
    **Chosen: (a)** — a dedicated ` _AGENT_INDEX_POLICY = WikiPolicy(..., opt_in_tag=None)`
    keeps the field on the policy object (one source of truth per type), at the cost of one
    extra frozen instance. Option (b) would re-introduce the string-matching ADR-0209 removed.

- `is_recall_visible(page: dict, opt_in_tags: Sequence[str] | None = None) -> bool`
  (`yadgar/_shared/wiki/policy.py:163`) — rewrite the exclusion arm (`:186-190`):
  ```python
  policy = get_policy(page.get("page_type"))
  if policy.recall_disposition != "exclude":
      return True
  if not opt_in_tags or policy.opt_in_tag is None:
      return False
  return policy.opt_in_tag in set(opt_in_tags)
  ```
  The page's own `tags` field is NO LONGER read for the exclusion gate. The docstring (`:163-185`)
  is updated: "the opt-in is PER TYPE: an excluded page survives only when the caller's tags
  include its page_type's declared `opt_in_tag`. A type with `opt_in_tag=None` is excluded
  unconditionally — no tag unlocks it."

**Existing signatures to preserve (verified):**

- `WikiProvider.candidates(self, query, scope, limit) -> list[Candidate]`
  (`yadgar/backend/retrieval/providers/wiki.py:60`) — unchanged; the call site at `:100`
  (`if not is_recall_visible(page, self._tags): continue`) already passes `self._tags` and needs
  no edit. The behavior change is entirely inside `is_recall_visible`.
- `wiki_query(...)` (`yadgar/core/server/tools/wiki.py:523`) — unchanged; the call site at `:590`
  (`results = [r for r in results if is_recall_visible(r, tags)]`) already passes `tags` and needs
  no edit.
- `get_policy(page_type: str | None) -> WikiPolicy` (`:147`) — unchanged signature; returns a
  `WikiPolicy` that now carries `opt_in_tag`.

## 4. Build steps (TDD)

1. **RED** — extend `tests/backend/test_wiki_provider_policy_exclusion.py`:
   - New `test_opt_in_must_match_type_own_tag`: an `agent_pattern` page tagged
     `["agent-prompt", "yadgar"]` is DROPPED under `tags=["yadgar"]` (not the type's opt-in tag)
     and SURVIVES under `tags=["agent-prompt"]`. This is the direct reversal of the current
     `test_opt_in_is_tag_intersection` (`:110`) and is the load-bearing assertion of C1.
   - New `test_toc_unconditional_exclusion`: the `agent_index` TOC is dropped under bare recall,
     under `tags=["agent-prompt"]`, AND under `tags=["agent-prompt-toc"]` (its own tag) —
     `opt_in_tag=None` means no tag unlocks it.
   - Revise `test_legacy_agent_prompt_type_still_excluded` (`:129`): the page now SURVIVES
     `tags=["agent-prompt"]` (type's opt-in tag) and is DROPPED under bare recall. Rename to
     `test_legacy_type_survives_own_opt_in_tag`.
   - All three RED before implementation.
2. **GREEN** — add `WikiPolicy.opt_in_tag` field; add `_AGENT_INDEX_POLICY` with
   `opt_in_tag=None`; set `opt_in_tag="agent-prompt"` on `_AGENT_LIBRARY_POLICY`; rewrite
   `is_recall_visible` per §3.
3. **RED** — extend `tests/core/test_wiki_query_policy_exclusion.py`: revise
   `test_opt_in_is_tag_intersection` (`:88`) so `tags=["quokka"]` yields only
   `{"plain-quokka-page"}` (the two excluded pages no longer unlock on a non-opt-in tag); add
   `test_type_opt_in_tag_unlocks` asserting `tags=["agent-prompt"]` reaches the agent_pattern
   page but NOT the TOC.
4. **GREEN** — no further code change needed (same `is_recall_visible` serves both paths); the
   core test confirms the wiki_query call site (`wiki.py:590`) inherits the new rule without edit.
5. **REFACTOR** — collapse the `_AGENT_LIBRARY_POLICY` + `_AGENT_INDEX_POLICY` pair if a shared
   base proves cleaner; verify the `WikiPolicy` field-order collision with Car J (see §6) and
   lock the field number.
6. **GATE** — run `ruff`, `import-linter`, `check_versions`, `check_backend_bump` (expect
   "no backend build inputs staged" pass since `_shared` ≠ backend), and the two revised test
   modules.

## 5. Acceptance gates

- [ ] `WikiPolicy.opt_in_tag` is appended with default `None`; existing 4–5-arg positional
  `WikiPolicy(...)` callers (tests at `test_wiki_provider_policy_exclusion.py` and elsewhere)
  still construct without error.
- [ ] `is_recall_visible` (`policy.py:163`) no longer reads `page.tags` for the exclusion gate;
  an excluded page survives iff `policy.opt_in_tag is not None and policy.opt_in_tag in opt_in_tags`.
- [ ] `agent_index` (TOC) is excluded unconditionally — dropped under bare recall, under
  `tags=["agent-prompt"]`, and under `tags=["agent-prompt-toc"]`.
- [ ] `agent_pattern` / `agent_discipline` / legacy `agent_prompt` survive ONLY under
  `tags=["agent-prompt"]` (or a superset containing it); a non-opt-in tag (`yadgar`, `quokka`)
  does NOT unlock them even when the page carries that tag.
- [ ] Both search paths inherit the new rule with no call-site edit: backend
  `providers/wiki.py:100` and core `wiki.py:590`.
- [ ] core version bumped per WORKFLOW RULE (pyproject `version` → `server.json` `version` via
  `scripts/sync_version.py`). `BACKEND_VERSION` (`yadgar/__init__.py:21`) bump is NOT required
  unless `yadgar/backend/**` is touched — `scripts/check_backend_bump.py:44`
  `BACKEND_BUILD_DIRS = ("backend",)` excludes `_shared`.
- [ ] pre-commit green (ruff, import-linter, I32, I33, `check_versions`)
- [ ] tests pass (`tests/backend/test_wiki_provider_policy_exclusion.py`,
  `tests/core/test_wiki_query_policy_exclusion.py`)

## 6. Sequencing

- **Depends on: —** (§7 row C1). C1 is rootless — it touches only `_shared/wiki/policy.py` and
  tests, no ledger/migration/registry prerequisite.
- **WikiPolicy field-order collision with Car J.** Car J (`docs/plans/0047-car-J-mutability-policy.md`)
  also appends a field to `WikiPolicy` (it claims "field #6 `mutability`"). C1 and J are
  independent (J depends on A; C1 depends on nothing) so either can land first. Whichever lands
  second MUST take the next field number — both append with defaults so there is no test breakage,
  but the field-order ambiguity should be resolved at merge time, not left to chance. Recommended:
  C1 lands first (no deps), takes `opt_in_tag` as field #6; J takes `mutability` as field #7.
- **Coordinates with C2 (downweight) and C3 (identity gate).** All three live on `WikiPolicy` /
  `policy.py`. C2 implements the `recall_disposition="downweight"` code path (currently a docstring
  only, `policy.py:34,67`); C3 reimagines `gate_mode="identity"` (D21, currently dead code per
  §1.4). They do not block C1 but share the resolver surface — keep `is_recall_visible` and
  `get_policy` changes minimal so C2/C3 can extend without rework.
- **Gates nothing directly** — D24's "agent_prompt excluded unconditionally, which kills the
  tag-override" is realized by Car I (deletes the TOC, agent-prompt table) + Car G (retype to
  `adr_superseded`). C1 is the narrowing that makes the eventual kill safe: until Car I ships,
  `recall(tags=["agent-prompt"])` must still reach agent-pattern pages, and C1 ensures it reaches
  ONLY them (not any excluded page carrying an unrelated tag).

## 7. ADRs / decisions

- **D22** — `recall_disposition` becomes status-driven: accepted/open → include;
  superseded/rejected/deprecated → exclude; task → downweight; agent_prompt → exclude
  unconditional. C1 is the opt-in-tag mechanism that makes "exclude" enforceable per-type rather
  than per-page-tag; the unconditional-exclusion end state is `opt_in_tag=None`.
- **D24** — agent-prompt discovery is `agent_prompt_list` + `agent_prompt_get`, not recall. "This
  is what lets `agent_prompt` be excluded unconditionally, which is what kills the tag-override."
  C1 is the stepping stone: it does not kill the override, it narrows it to the type's own tag so
  the kill in Car I is a flip of `opt_in_tag` to `None`, not a behavioral rewrite.
- **ADR-0209** — page_type is the policy lever, not tags. C1 aligns the opt-in with that lever:
  the unlock key is the TYPE's declared tag, not the page's tag set. The pre-C1 `test_opt_in_is_
  tag_intersection` docstring (`test_wiki_provider_policy_exclusion.py:113-115`) explicitly says
  "Deliberately NOT 'only a tag that names the excluded family' — that would need a tag→page_type
  map, i.e. the string-matching ADR-0209 removes." C1 REVERSES that deliberate choice, on the
  authority of §1.4's defect finding and D24's direction: the string-matching ADR-0209 removes is
  exactly the string-matching C1 now introduces, because the alternative (any-tag intersection)
  is the defeat-of-exclusion §1.4 documents.

## 8. Out of scope

- **Killing the agent-prompt tag-override entirely** (D24's end state) — that is Car I (delete TOC
  + `agent_prompt` table + `agent_prompt_list`/`agent_prompt_get`) and Car G (`adr_superseded`
  retype). C1 keeps `opt_in_tag="agent-prompt"` on the three library types so the documented
  `recall(tags=["agent-prompt"])` lookup keeps working until Car I replaces it.
- **Implementing `downweight`** — that is C2 (3b). C1 does not touch the `downweight`
  disposition; `is_recall_visible` treats `downweight` as include (it only special-cases
  `exclude`).
- **Reimagining the identity gate** — that is C3 (3c, D21). C1 does not touch `gate_mode`.
- **Status-driven `recall_disposition`** (D22's accepted/open/superseded logic) — that requires
  the ledger's `status` column (Car A) and the `adr_superseded` retype (Car G). C1 is
  type-centric ONLY; it does not read page status. A superseded ADR today is excluded only if its
  `page_type` is one C1 marks excluded; the status-driven flip lands later.
- **Per-page opt-in overrides** — C1 is per-type. A page cannot declare its own opt-in tag
  distinct from its type's. If that need arises it is a separate car.
- **The SQL pre-filter / HNSW ranking path** — `WikiStore.query(include_tag=...)`
  (`providers/wiki.py:72-78`) uses `tags` for ranking pre-filter; C1 does not touch it. The
  exclusion gate is a post-rank Python filter; the two are independent.

## 9. Risks / open questions

- **[VERIFY: field-order collision with Car J]** — both C1 and J append a field to `WikiPolicy`.
  The frozen dataclass tolerates this (both default-bearing), but the "field #6" claim in Car J's
  doc (`docs/plans/0047-car-J-mutability-policy.md` §3) collides with C1's `opt_in_tag` if C1
  lands first. Resolve at merge: C1 = field #6 `opt_in_tag`, J = field #7 `mutability`, OR vice
  versa. No functional impact either way; documented here so the second merger does not blindly
  reuse #6.
- **[VERIFY: behavior change is intended]** — C1 reverses a DELIBERATE task-0134 design decision
  documented in `test_wiki_provider_policy_exclusion.py:113-115`. The reversal is authorized by
  §1.4 ("the tag-override defeats exclusion") and D24 ("excluded unconditionally … kills the
  tag-override"), but it IS a reversal — the two `test_opt_in_is_tag_intersection` tests must be
  rewritten, not preserved. If a future audit treats test-rewrite as regression, point at this
  doc and §1.4.
- **[VERIFY: `opt_in_tag` vs page tags for SQL pre-filter]** — `WikiProvider.candidates` passes
  `include_tag=self._tags[0]` to `WikiStore.query` (`providers/wiki.py:72`). Under C1, a
  `recall(tags=["agent-prompt"])` call still pre-filters on the `agent-prompt` tag at the SQL
  layer, so an agent_pattern page that does NOT carry the `agent-prompt` tag would be EXCLUDED by
  the SQL pre-filter before `is_recall_visible` ever sees it — even though C1's type-centric gate
  would have let it through. This is ACCEPTABLE (agent_pattern pages SHOULD carry the
  `agent-prompt` tag, and ADR-0209 makes `page_type` the lever not tags), but it means C1's
  type-centric unlock is only observable when the SQL pre-filter is absent (e.g. `wiki_query`
  with no `include_tag`, or a brute-force cosine path). Confirm the interaction is intended; if
  the SQL pre-filter must also become type-aware, that is a larger change and likely belongs in
  C3 (identity gate) or a follow-up.
- **[VERIFY: `_AGENT_LIBRARY_POLICY` split]** — giving `agent_index` its own policy instance means
  `POLICY_BY_TYPE` no longer has a clean "all library types share one instance" invariant. The
  alternative (a separate `OPT_IN_TAG_BY_TYPE` dict) was rejected in §3 to avoid re-introducing
  string-matching. Confirm the split is acceptable; the `_AGENT_LIBRARY_POLICY` docstring
  (`:114-121`) should be updated to note the TOC is split out for its `opt_in_tag=None`.
