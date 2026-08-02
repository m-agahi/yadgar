# Plan: retire the CE residue that reads as live config, and the systemd claim that was never true

**Date:** 2026-08-01
**Task:** #0121
**ADR:** ADR-0191 (new, small). Touches ADR-0043, ADR-0067, ADR-0104. Supersedes nothing.
**AS BUILT:** the ADR index assigned **ADR-0192**, not 0191 — 0191 was taken by the
time this car ran. Every "ADR-0191" below means ADR-0192; the shipped code and docs
all cite 0192.
**Status:** design proposed, not started. Small-to-medium car; two independent work items plus one guard.

---

## 0. Why this car exists

The main thread twice told the user their CE reranker was `ms-marco-MiniLM-*`, contradicting the
user, who was right. Nobody misread the code. The code contains, in the two places a reader looks
first, a model name that is not the reranker:

```
yadgar/_shared/config/config.py:189   CROSS_ENCODER_MODEL: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
yadgar/_shared/config/config.py:190   CROSS_ENCODER_ENABLED: bool = True  # FlashRank ONNX is fast enough for CPU
~/.config/yadgar/config.yaml:175-178  #  Cross-encoder model name
                                      cross_encoder_model: cross-encoder/ms-marco-MiniLM-L-6-v2
                                      #  Enable FlashRank ONNX cross-encoder reranking
                                      cross_encoder_enabled: true
```

Both the field name and the generated comment assert that this is *the* cross-encoder and that it
runs on FlashRank ONNX. Neither is true. The generated comment is worse than the field, because it
is produced from `config_yaml.py`'s `FIELD_META` desc and is therefore reproduced verbatim into
every operator's on-disk `config.yaml`.

The second item is the same defect in the other direction — a comment that names a mechanism the
repo has never had:

```
entrypoint-backend.sh:7-9   DB snapshots are handled outside the container by the systemd
                            ExecStartPre `cp -r` of the surrealkv data dir.
```

This is the sentence that made the user believe pre-migration backups existed, which is the whole
premise of car #0115.

**These are not bugs in behaviour. Every line of shipped code does the right thing.** They are
drifted artifacts that nothing tests, which the 203-defect audit
(`docs/plans/bug-cause-audit-203-defects-2026-08-01.md`) counts under DUPLICATION at 18.2% — the
largest architectural cause. §5 is the guard.

---

## 1. Ground truth — re-verified, build on this

| claim | evidence |
|---|---|
| primary CE is Ettin-32m | `config.py:293-294` (`GTE_RERANKER_ENABLED=True`, `GTE_RERANKER_MODEL="cross-encoder/ettin-reranker-32m-v1"`); loaded torch-fp32 by `_load_gte_reranker` (`yadgar/backend/ml_client/local_ml_client.py:36-52`); baked `Dockerfile.backend:55`; documented `README.md:447`, `docs/contracts/CAPABILITY_REGISTRY.md:291` |
| ONNX + flashrank were deliberately dropped | `pyproject.toml:159-165` (the `[onnx]` extra removed — optimum-onnx 0.1.0 caps `transformers<4.58.0`, Ettin needs `>=5.0`); `Dockerfile.backend:47-51` states flashrank is absent from pyproject/uv.lock and unimportable in prod |
| `AGENTS.md:184` "CE reranker (Ettin-32m)" | **correct — do not touch** |
| no `cp -r` ExecStartPre exists, or ever existed | `yadgar/core/daemon/systemd.py:186-188` and `scripts/install/yadgar-backend.service.in:30-32` emit only stop/rm/network-create; `flake.nix:368-372,429-440` emit stop/rm/network-create/mkdir; `git log -S 'ExecStartPre=cp -r' --all` and `-S 'ExecStartPre=/bin/cp' --all` both return **nothing** |

Note for anyone grepping: **`grep -i ettin` matches the substring inside `Settings`.** Use
`grep -w Ettin` or a case-sensitive pattern. Getting this wrong is how the original wrong answer
was produced.

---

## 2. Work item 1 — the CE residue, decided per item

The chain is `score_cross_encoder` (`local_ml_client.py:165-186`):
`_try_gte_reranker` → `_try_flashrank` → `_try_st_cross_encoder`. Three tiers, three different
verdicts. **Do not batch them.**

### 2.1 `_try_flashrank` — DELETE

`local_ml_client.py:93-116`. Hardcodes `model_name="ms-marco-MiniLM-L-12-v2"` and
`cache_dir=~/.cache/flashrank`.

**Provably unreachable in every shipped configuration.** `flashrank` is not in `pyproject.toml`
and not in `uv.lock`, so `from flashrank import Ranker, RerankRequest` (`:97`) raises
`ImportError`, caught at `:110-111` as a bare `pass`, and the function returns `None`. Deletion is
**behaviour-identical**: `None` → the caller advances to `_try_st_cross_encoder`, which is exactly
what happens today. The only environment where behaviour changes is one where an operator has
hand-installed an undeclared package into the venv, which is not a configuration this repo
supports or tests.

This is the brief's own argument made concrete: `Dockerfile.backend:51` calls this "the plan's
flashrank hedge", but **a rollback hedge that cannot import is not a hedge, it is a lie.** ADR-0067
already removed the onnx CE backend on precisely this reasoning ("dead opt-in: rejected default +
zero users = pure maintenance surface"); flashrank is the weaker case, because it was never even
opt-in-able.

Rejected alternative — *keep it and add `flashrank` to `pyproject`*: that ships a NEW live fallback
tier and a new supply-chain surface for a model nobody chose. `Dockerfile.backend:50-51` explicitly
scoped that out of the CE-swap train and the reasoning has not changed.

Rejected alternative — *keep it, annotate it dormant*: an annotated unreachable branch is still a
branch that must be read, kept compiling, kept `@observe`-decorated (`test_backend_observe_p3.py:179`
asserts that), and kept in the complexity baseline. The annotation cost recurs; the deletion is
once.

### 2.2 `GTE_RERANKER_FALLBACK_TO_FLASHRANK` — KEEP, but state what it actually does

`config.py:296`. Consulted at **exactly one place**: `local_ml_client.py:88`, *inside the
`except` block of `_try_gte_reranker`*. Two consequences nothing currently writes down:

* it is a **failure-mode selector**, not a flashrank switch: `False` ⇒ return
  `[0.0] * len(texts)` (terminal); `True` ⇒ fall through to the ST fallback. It has never selected
  flashrank, because flashrank has never been importable.
* when `GTE_RERANKER_ENABLED=False`, `_try_gte_reranker` returns `None` at the top guard
  (`:63-70`) and the flag is **never read at all** — you get the ST fallback regardless of its
  value.

After §2.1 lands there is nothing named flashrank left in the file for the name to gesture at,
while `~/.config/yadgar/config.yaml:277-278` on every real install says *"Fall back to FlashRank if
GTE reranker fails."* That is the same class of lie as the ms-marco one, so keeping the field
without rewriting its prose is not an option.

Rejected alternative — *rename to `GTE_RERANKER_FALLBACK_ENABLED`*: the env name
`YADGAR_GTE_RERANKER_FALLBACK_TO_FLASHRANK` is documented (`docs/reference/configuration.md:147`),
allowlisted (`yadgar/tests/config_env_only_allowlist.txt:107`), present in `FIELD_META`, and
already written into every operator's on-disk yaml. A rename needs an alias + a deprecation window
+ a migration, which is a car of its own. **KEEP-AND-ANNOTATE**, and record the rename as a
follow-up in the ADR's `revisit_trigger`.

### 2.3 `_try_st_cross_encoder` — KEEP-AND-ANNOTATE, do NOT delete

`local_ml_client.py:118-163`, reading `settings.CROSS_ENCODER_MODEL` at `:135` with an inline
literal fallback `"cross-encoder/ms-marco-MiniLM-L-6-v2"` at `:137`.

**This tier is genuinely reachable**, and that is the single most important correction to the
brief's framing. It runs whenever `_try_gte_reranker` returns `None`, i.e.:

* `GTE_RERANKER_ENABLED=False` — a legitimate operator kill-switch, and
* an Ettin load/predict failure with `GTE_RERANKER_FALLBACK_TO_FLASHRANK=True` (the default).

`sentence-transformers` **is** a declared dependency (`pyproject.toml:168`), so unlike flashrank
this import succeeds. Deleting it would remove the only path that yields non-zero scores when the
primary CE fails, in exchange for cosmetics. That trade is wrong.

But it is a **degraded** fallback and the degradation is deployment-dependent — say so at the site:

* `LocalMLClient` runs in two places (`ml_client.py:1-5`): inside the `yadgar-backend` container,
  and in host stdio/daemon mode.
* `cross-encoder/ms-marco-MiniLM-L-6-v2` is **not** baked (`Dockerfile.backend:54-62` bakes
  all-MiniLM-L6-v2, Ettin-32m, GTE-ModernBERT, doc2query only). In the container the fallback
  attempts an HF download; on a network-isolated host it fails into `record_exception` and returns
  zeros (`:147-151`). In host stdio/daemon mode with network it works normally.

So the honest annotation is: *reachable second-tier fallback; live in host mode, effectively
zero-scoring in the offline container; the live CE is `GTE_RERANKER_MODEL`.*

Rejected alternative — *repoint the ST fallback at `Alibaba-NLP/gte-reranker-modernbert-base`*
(which **is** baked, one cycle, as the ADR-0104 rollback): tempting, and it would make the fallback
work offline. But it changes runtime behaviour on a failure path with no measurement, silently
couples the fallback to a model the ADR says is scheduled for un-baking, and belongs behind a
quality gate. **Out of scope; record it in the ADR as the named follow-up.**

### 2.4 `CROSS_ENCODER_MODEL` — KEEP (it is read by live code), annotate hard

Read at `local_ml_client.py:135`. Since §2.3 keeps its reader, the field stays. The high-leverage
fix is the prose, not the field — see §2.6.

### 2.5 `CROSS_ENCODER_ENABLED` / `_TOP_K` / `_WEIGHT` — KEEP, all three are live

Verified live, not dormant:

| field | live readers |
|---|---|
| `CROSS_ENCODER_TOP_K` | `yadgar/backend/retrieval/_reranking_cross_encoder.py:108`, `yadgar/backend/retrieval/fusion.py:331` |
| `CROSS_ENCODER_ENABLED` | `_reranking_cross_encoder.py:111`, `fusion.py:356`, `local_ml_client.py:124` |
| `CROSS_ENCODER_WEIGHT` | `_reranking_cross_encoder.py:149` |

**A real semantic overload found while verifying, flagged not fixed:** `CROSS_ENCODER_ENABLED` is
read with two different meanings — at `_reranking_cross_encoder.py:111` it gates the CE rerank
*stage* in the pipeline; at `local_ml_client.py:124` it gates the *ST fallback model load* inside
`_try_st_cross_encoder`. The second is coincidental reuse of a name. Splitting it is a behaviour
change on a kill-switch and does not belong in a comment-correctness car. Record it in the ADR
consequences so the next reader does not have to rediscover it.

### 2.6 The prose surfaces — this is the actual fix for the incident

| file:line | current text | action |
|---|---|---|
| `yadgar/_shared/config/config.py:190` | `# FlashRank ONNX is fast enough for CPU` | **rewrite.** Names a subsystem that cannot import. This one line is the most misleading in the repo on this topic |
| `yadgar/_shared/config/config.py:188` | `# v10: Cross-encoder reranking settings` | extend: point at `GTE_RERANKER_*` (`:290-296`) as the live CE slot |
| `yadgar/_shared/config/config_yaml.py:210-213` | `"desc": "Enable FlashRank ONNX cross-encoder reranking"` | **rewrite — highest leverage in the car.** This desc is what generates `~/.config/yadgar/config.yaml:177` on every install |
| `yadgar/_shared/config/config_yaml.py:214` | `"desc": "Cross-encoder model name"` | rewrite: "Degraded-mode fallback CE model. The live reranker is `gte_reranker_model`." Mirrors the wording already correct at `docs/reference/configuration.md:141` |
| `yadgar/_shared/config/config_yaml.py:229-231` | `"desc": "Fall back to FlashRank if GTE reranker fails"` | rewrite per §2.2 |
| `docs/contracts/CAPABILITY_REGISTRY.md:278` | `explanation:` says "default FlashRank ONNX, fast on CPU" | **rewrite.** `check_capability_coverage.py` cannot see prose; `check_registry_prose_liveness.py` checks identifier liveness, and `flashrank` is a live identifier until §2.1 lands, so **neither guard fires today** |
| `docs/reference/tributes.md:53` | FlashRank row, "Lightweight ONNX reranker fallback in the CE chain" | **delete the row.** It is a licence-attribution table; flashrank has never been a dependency, so the row is wrong on attribution grounds independently of this car |
| `docs/reference/configuration.md:147` | already hedged ("effectively inert") | tighten to §2.2's true semantics |
| `benchmarks/test_e_locomo.py:340-342`, `benchmarks/run_locomo_ablation.py:93-97` | FlashRank comments in ablation labels | fix the comments. Cheap, and §5's guard scans code comments — leaving them makes it land red |

### 2.7 Adjacent finding — the NLI slot has the same shape

Not this car's incident, but the same family, and one of three §5.4 rule-1 customers (alongside
`CROSS_ENCODER_MODEL` and `COMET_MODEL`), plus the only genuine rule-2 failure:

* `config.py:300` — `NLI_MODEL: str = "cross-encoder/nli-deberta-v3-base"`
* `local_ml_client.py:202` — inline fallback literal `"cross-encoder/nli-deberta-v3-small"`
* `README.md:554` and `docs/reports/audits/license-compliance-audit-2026-05-30.md:63` — list
  `-small` in the licence tables; `docs/reference/tributes.md:31` lists `-base`

Three surfaces, two models. `NLI_RERANKING_ENABLED` defaults `False` (`config.py:299`) so nothing
loads in prod and nothing has ever caught it. **Do not fix the licence tables in this car** —
picking which one is right is a licence question, not a code question. Fix only
`local_ml_client.py:202` to read the same default as the field, and file the licence-table
divergence as a separate item.

---

## 3. Work item 2 — `entrypoint-backend.sh:4-11`

The comment carries **three** false claims, not one. Written 2026-05-12 in `267a45c3`
(*"fix(backend): drop /export backup loop, raise worker stack size (#43)"*).

1. **`:7-9` — "DB snapshots are handled outside the container by the systemd `ExecStartPre` `cp -r`
   of the surrealkv data dir."** No such directive exists anywhere, and `git log -S` proves it
   never did (§1). This is the sentence car #0115 exists because of.
2. **`:4` — "no in-container backup loop."** False since `_wiki_backup_loop` was added — in **this
   same file**, `:280-307`, a 24 h loop writing `/data/backups/wiki/wiki_<TS>.jsonl` with 14-day
   retention (ADR-0076 D3).
3. **`:9-11` — "run `surreal export` … only after the upstream export-recursion issue is
   resolved."** Contradicted twice over. `SURREAL_RUNTIME_STACK_SIZE=33554432` (this file, `:138`)
   was raised in the *same commit* as the prohibition, and
   `yadgar/core/scripts/nightly_cycle.py:261-266` → `yadgar/core/backup/backup.py:65,75-78` now
   calls `GET /export` **against a live backend by design**, labelled `nightly-pre` /
   `nightly-post`.

### 3.1 What is actually true today

| mechanism | where | shape |
|---|---|---|
| pre-vacuum physical snapshot | `yadgar/core/vacuum/phases.py:148-158` | host-side core process; `svc.stop()` quiesces, then `shutil.copytree(db_path, surreal_db.pre-vacuum-<TS>)` |
| nightly logical snapshots | `yadgar/core/scripts/nightly_cycle.py:261`, `yadgar/core/backup/backup.py:65` | `GET /export` → `.surql`, transactionally consistent against a live backend |
| in-container wiki snapshot | `entrypoint-backend.sh:280-307` | targeted `SELECT * FROM wiki_page` via `/sql`, deliberately **not** `/export` |
| **pre-migration snapshot** | **none** | the gap car #0115 exists to close |

### 3.2 Do NOT forward-reference #0115

The brief asks to point the corrected comment at 0115 "once that lands". **The corrected comment
must not name 0115's mechanism**, for the same reason the original comment is being fixed: 0115 is
unlanded and itself gated behind #0027c and #0046, so a comment describing it would be false in
exactly the way this car is repairing. Write it true-as-of-today — "no pre-migration snapshot
exists (task 0115)" — and let 0115's own commit add its line. Say this explicitly in the commit
message so the next reader does not "helpfully" add the forward reference back.

---

## 4. The cascade — exact ordered edit list

Removing or touching anything here is not a one-line change. Two independent cascades.

### 4.1 The backend-version cascade — fires on BOTH work items

`scripts/check_backend_bump.py:41-45` declares `BACKEND_BUILD_INPUTS = ("entrypoint-backend.sh",
"Dockerfile.backend")` and `BACKEND_BUILD_DIRS = ("backend",)`, matched at **any depth**
(`:66-70`). So:

* item 1 edits `yadgar/backend/ml_client/local_ml_client.py` → matches `backend` at depth 2
* item 2 edits `entrypoint-backend.sh` → matches by name

The hook is `always_run: true` (`.pre-commit-config.yaml:46-57`) and it evaluates the **cumulative
branch diff vs merge-base(origin/master)**, so it fires on every commit of the branch, not only the
one that stages a backend file. **Both items require bumping `backend_version` in `server.json:11`
(currently `5.60.0`) plus a CHANGELOG row.** A "one-line comment fix" that cannot commit is how
this plan gets rejected at implementation time.

`Dockerfile.backend:67`'s `LABEL version="5.0.0"` is a separate, already-stale literal that
`check_backend_bump` does **not** read. Leave it; do not opportunistically "fix" it (§ house rule
on adjacent code) — file it if it bothers you.

### 4.2 Deleting `_try_flashrank` — ordered, with the guard that fires if you skip a step

| # | edit | guard that fires if missed |
|---|---|---|
| 1 | `local_ml_client.py:93-116` — delete `_try_flashrank` | — |
| 2 | `local_ml_client.py:182` — drop the chain call | ruff (undefined attribute) / `test_ml_client.py` |
| 3 | `local_ml_client.py:31` — drop `self._flashrank_ranker = None` | — |
| 4 | `local_ml_client.py:292-293` — drop the eviction branch in `unload_if_idle` | — |
| 5 | `local_ml_client.py:22, 170, 260` — docstrings naming flashrank | §5 guard (once landed) |
| 6 | `yadgar/tests/core/test_backend_observe_p3.py:179` — remove `_try_flashrank` from the expected-`@observe` tuple | **HARD BREAK.** `getattr(mc.LocalMLClient, "_try_flashrank")` raises `AttributeError`. Gate: **test-core** |
| 7 | `yadgar/tests/core/test_idle_eviction_flip.py:13` (docstring), `:238`, `:335` (`patch.dict(sys.modules, {"flashrank": None})`) | `:238`/`:335` keep passing but become vacuous — **convert or delete, do not leave green-and-vacuous.** Gate: **test-core** |
| 8 | `yadgar/tests/backend/test_model_load_smoke.py:77-93` — delete `test_flashrank_loads_and_scores`; `:17` gate-map docstring | always-skipped (`importorskip`), pins a dead constant. Gate: **test-backend** |
| 9 | `yadgar/tests/skip_inventory.json:112` — the `note` text explicitly covers "the flashrank importorskip" | `scripts/check_skip_inventory.py --validate-inventory` (pre-commit **and** CI `.forgejo/workflows/ci-pr.yaml:371`, ADR-0087 stale-entry governance) |
| 10 | `.complexity-baseline.json` — run `python scripts/check_complexity.py --update-baseline yadgar/backend/ml_client/local_ml_client.py`, **then delete the dead `…local_ml_client.py::_try_flashrank@94` key by hand** (`update_baseline` merges into the existing dict rather than rewriting it, so the orphan row survives the regen; harmless — only `.complexity-allowlist.json` has a stale check — but leaving it is the same residue class this car is about) | **non-obvious and load-bearing.** Baseline keys are `path::name@lineno` (`check_complexity.py:219-231`). Deleting a function at `:94` shifts the lineno of **every** later entry — `_try_st_cross_encoder@120`, `score_cross_encoder@166`, `score_nli@189`, `score_pair@243`, `unload_if_idle@249` — so their baseline lookups miss and any that exceed a soft limit are reclassified as NEW violations. Nothing about the error message will point at flashrank |
| 11 | `docs/CHANGELOG.md` — Unreleased row | `scripts/check_changelog_unreleased_versions.py` |

`GTE_RERANKER_FALLBACK_TO_FLASHRANK` survives §2.2, so `config_env_only_allowlist.txt:107` and
`test_ml_client.py:298` need **no** change.

### 4.3 The Settings-field-removal recipe — worked in full, and why it says KEEP

`CROSS_ENCODER_MODEL` is the only removal candidate in the car. §2.3/§2.4 keep it, but the recipe
is what proves the decision rather than assuming it, and it is the reusable artifact:

| # | edit | guard |
|---|---|---|
| 1 | `config.py:189` — delete the field | — |
| 2 | `config_yaml.py:214` — delete `FIELD_META["cross_encoder_model"]` | I25 `test_all_settings_fields_covered` (`yadgar/tests/server/test_config_three_way_sync.py:158`), pre-commit `check-config-three-way-sync` (`always_run`) + CI `ci-pr.yaml:410-419` |
| 3 | `config_registry.py` — **no edit.** `grep -n cross_encoder` returns nothing; the field is FIELD_META-only, which is why it sits in the I25 Tier-2 backlog | — |
| 4 | `yadgar/tests/config_env_only_allowlist.txt:83` — delete `YADGAR_CROSS_ENCODER_MODEL` | **NOTHING FIRES.** I25 has four tests — `test_allowlist_file_exists`, `test_all_settings_fields_covered`, `test_allowlist_entries_have_yadgar_prefix`, `test_tier1_entries_have_valid_reason` — and **none checks for a stale allowlist entry**. A line naming a deleted field rots silently. Real gap; see §5.4 |
| 5 | `docs/contracts/CAPABILITY_REGISTRY.md:272` — drop `CROSS_ENCODER_MODEL` from CAP-RETR-014's `settings:` | **fires.** `_check_coverage` (`scripts/check_capability_coverage.py:298-305`) is bidirectional: `STALE setting ref: registry cites X but it's not in config.py`. Pre-commit + CI `ci-pr.yaml:425` |
| 6 | `docs/reference/configuration.md:141` — delete the row | none (prose) |
| 7 | `local_ml_client.py:135-138` — the reading site | ruff |
| 8 | operator `~/.config/yadgar/config.yaml:176` | **no action needed, and this is the one blocker that turns out not to exist.** `YamlConfigSource.__call__` (`config.py:47-53`) filters `if k in self.settings_cls.model_fields`, so an unknown key on an existing install is **silently dropped**, not a validation error |
| 9 | `check_registry_prose_liveness` allowlist | no entry needed — `CROSS_ENCODER_MODEL` appears only in the registry's `settings:` field, which that lint does not read (it scans `explanation:`/`wiring:` prose) |

**Verdict: KEEP.** Nine coordinated edits across five contract surfaces, one of which (step 4) has
no guard at all, to remove a field that live code reads (§2.3). The prose fix in §2.6 buys the
whole benefit at a fraction of the risk.

### 4.4 The prose + comment edits

Item 1 prose: the §2.6 table, in one commit. Item 2: rewrite `entrypoint-backend.sh:4-11` per §3.1
and §3.2. Both then need §4.1's `server.json` bump.

---

## 5. Anti-recurrence — one guard

### 5.1 The criterion, stated before the choice

The guard must fire on **the motivating defect**: a model id, in a live Settings field, that reads
as the CE and is not. A guard whose "what it cannot catch" section has to admit it misses the
incident is not the guard for this car.

### 5.2 Rejected candidates

| candidate | why not |
|---|---|
| **removed-package prose lint** — closed vocabulary of deliberately-dropped packages (`flashrank`, `optimum-onnx`, `onnxruntime`) scanned across comments/docs, fires unless marked historical | Cheap and zero-FP, and it *would* catch `config_yaml.py:211`'s "Enable FlashRank ONNX…". But it does not touch `cross_encoder_model: …ms-marco…`, which is the defect. **Fails §5.1.** Worth building later as a deps-train companion; not this car |
| **"comments naming a systemd directive must match a directive the generators emit"** | Catches work item 2 exactly, and is the right shape for that half. But extracting "the claimed directive" from free prose is fuzzy — `ExecStartPre` appears in explanatory comments that describe *other* units (`yadgar.service.in:40`), so a naive matcher produces false positives on correct text. Needs a prose-annotation convention nobody has adopted. Rejected on FP risk |
| **"no `Settings` field is read only from unreachable branches"** | Requires real reachability analysis (import-availability + config-value propagation). Not cheap, and it would have concluded *nothing* here: `CROSS_ENCODER_MODEL` **is** read from a reachable branch (§2.3). It answers the wrong question |
| **extend `check_registry_prose_liveness.py` to the config surface** | Its liveness test is "does this identifier exist in executable code", and `flashrank` is a live identifier until §2.1 lands. It would pass today on the exact text that misled a reader. The script's own docstring names this ceiling |

### 5.3 Chosen: `scripts/check_model_id_liveness.py`

**Rule 1 — every `*_MODEL` Settings default must be baked, or allowlisted with a rationale.**

* Left side: AST-scan `config.py`'s `Settings` class body for fields whose **name ends in `_MODEL`**
  and whose default is a string constant. Reuse `enumerate_settings` (`check_capability_coverage.py:82-110`)
  — same file, same AST walk, already proven.
* Right side: parse the quoted string literals out of `Dockerfile.backend`'s `RUN python -c` blocks
  (`:54-62`) — the bake list.
* Compare on the id **after the last `/`**, so `all-MiniLM-L6-v2` matches
  `sentence-transformers/all-MiniLM-L6-v2`. (Theoretical collision: two orgs publishing the same
  bare name. Accepted, stated.)
* **Skip empty-string defaults.** `IMPLICIT_EMBEDDING_MODEL = ""` (`config.py:314`) is a sentinel
  for an unimplemented feature, not a model id. Without this rule the guard fires spuriously on
  day one (§5.4).
* Outcome per field: **BAKED** → pass. **ALLOWLISTED** in `.model-id-allowlist.json` with a
  rationale ≥40 chars → pass. Otherwise → **FAIL**.
* Stale-entry rule, mirroring `.registry-prose-allowlist.json` and `.complexity-allowlist.json`
  (I30): an allowlist row for a field that is now baked, or that no longer exists, is a **hard
  error**. This is what stops the allowlist becoming a write-only dump.

**Rule 2 — no orphan model-id literal in the ML-loading modules.** Any string literal matching
`^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$` (exactly one slash, no spaces) must
appear in the vocabulary assembled by rule 1 (Settings defaults ∪ bake list ∪ allowlist),
otherwise FAIL.

**The scan set is the whole design, so state it precisely.** A hand-picked list of model-org
prefixes (`cross-encoder/`, `BAAI/`, …) is a *silent coverage hole*, not an FP source — a missed
prefix never shows up when the guard runs clean. Measured: a prefix list of four would have missed
`nomic-ai/` and `mismayil/`, both present in this repo. So do not filter by prefix. Filter by
**module**:

```
scan set = { files under yadgar/** (excluding yadgar/tests/**) that reference
             sentence_transformers or transformers }
         ∪ yadgar/backend/ml_client/**
         ∪ yadgar/backend/embed_service/**
         −  yadgar/_shared/config/config.py        # this file IS the vocabulary
```

Six files match the transformers reference today (`reranking.py`, the three `ml_client/` modules,
`embeddings.py`, `_shared/enrichment/_seq2seq.py`); the two package clauses add
`embed_service_config.py`. **Measured FP rate on the current tree: zero** — no MIME type
(`application/json` ×20, `text/plain` ×14), no git ref (`origin/master`, `feat/x`), no
`anthropic/alwaysLoad`, no `React/TSX` reaches it, because none of those live in an ML-loading
module. The naive whole-repo variant of the same regex picks up ~50 such hits and would need a MIME
exclusion list; the module narrowing removes the need for one.

**Stated blind spot of the narrowing:** a hardcoded model id in a module that neither imports
transformers nor sits in those two packages is invisible. One exists today and is worth watching —
`yadgar/backend/embed_service/embed_service_config.py:151` hardcodes
`"cross-encoder/ettin-reranker-32m-v1"` as a `resolve_knob` fallback, a **third** copy of the
primary CE id whose own comment says *"Fallback kept in sync with the config default"* by hand. The
`embed_service/**` clause above is there specifically to keep it in scope.

**Placement.** `scripts/check_model_id_liveness.py`, wired into `.pre-commit-config.yaml` and the
CI `invariant-checks` job next to `check-capability-coverage` (`ci-pr.yaml:425`). Its test goes in
`yadgar/tests/scripts/` — the **test-fast** gate (`ci-pr.yaml:79`).

### 5.4 The measured day-one failure set — enumerated, not estimated

Both sides were enumerated against the current tree. These are counts, not guesses; the
allowlist-seed cost below is what an implementer actually pays.

**Rule 1 — left side is exactly seven `*_MODEL` Settings fields** (`config.py`), right side is the
four ids baked at `Dockerfile.backend:54-62` (`all-MiniLM-L6-v2`, `cross-encoder/ettin-reranker-32m-v1`,
`Alibaba-NLP/gte-reranker-modernbert-base`, `doc2query/msmarco-t5-small-v1`):

| field | default | verdict |
|---|---|---|
| `EMBEDDING_MODEL` (`:67`) | `all-MiniLM-L6-v2` | BAKED — pass |
| `GTE_RERANKER_MODEL` (`:294`) | `cross-encoder/ettin-reranker-32m-v1` | BAKED — pass |
| `DOC2QUERY_MODEL` (`:258`) | `doc2query/msmarco-t5-small-v1` | BAKED — pass |
| `CROSS_ENCODER_MODEL` (`:189`) | `cross-encoder/ms-marco-MiniLM-L-6-v2` | **FAIL → allowlist** |
| `NLI_MODEL` (`:300`) | `cross-encoder/nli-deberta-v3-base` | **FAIL → allowlist** (default-OFF, `NLI_RERANKING_ENABLED=False`) |
| `COMET_MODEL` (`:252`) | `mismayil/comet-bart-ai2` | **FAIL → allowlist** (default-OFF, `COMET_*_ENABLED=False`) |
| `IMPLICIT_EMBEDDING_MODEL` (`:314`) | `""` | **spec edge case** — an empty default is a sentinel, not a model id (`:312-313`: *"CONFIG-ONLY pending future DualCSE implementation"*). The guard must skip empty-string defaults or it fires spuriously on day one |

**Rule 1 seed: 3 allowlist rows + 1 skip rule.** Not two. `COMET_MODEL` is the third, and it is the
one nobody would have predicted.

The `CROSS_ENCODER_MODEL` row's rationale must read approximately *"reachable only as the degraded
ST fallback when the Ettin primary is disabled or fails; deliberately not baked, so in the offline
container it yields zeros. The live CE is `GTE_RERANKER_MODEL`."* **That sentence is precisely the
one whose absence caused the incident, and it now lives in a machine-checked file that a stale-entry
rule stops from rotting.**

**Rule 2 — nine literals in the scan set**, three distinct offenders:

| hit | verdict |
|---|---|
| `local_ml_client.py:137` `cross-encoder/ms-marco-MiniLM-L-6-v2` | in vocabulary (it is `CROSS_ENCODER_MODEL`'s own default, duplicated as a defensive inline fallback) — **pass**. Rule 2 deliberately does not flag duplication of a *known* default; that shape is everywhere and flagging it is noise |
| `embed_service_config.py:151` `cross-encoder/ettin-reranker-32m-v1` | in vocabulary — pass |
| `local_ml_client.py:202` `cross-encoder/nli-deberta-v3-small` | **FAIL — the genuine catch.** Disagrees with `NLI_MODEL`'s `-base` default (§2.7) |
| `embeddings.py:36,44` `BAAI/bge-small-en-v1.5` · `:37,45` `BAAI/bge-base-en-v1.5` · `:38,43,48` `nomic-ai/nomic-embed-text-v1.5` | **FAIL ×7, 3 distinct → allowlist.** Legitimate: `MODEL_DIMENSIONS` / `MODEL_QUERY_PREFIX` / `MODEL_DOC_PREFIX` are reference tables for embedding models an operator may select via `EMBEDDING_MODEL`. Rationale: *"operator-selectable embedding model; dimension/prefix reference data, not a hardcoded default"* |

**Rule 2 seed: 3 allowlist rows, 1 genuine failure.**

**Total implementer cost: 6 allowlist rows, 1 skip rule, 1 real fix (`local_ml_client.py:202`).**
Say that in the PR body rather than "two rows".

**Cannot catch — state this in the script's docstring, in the `check_registry_prose_liveness.py`
house style:**

* **prose.** Neither the FlashRank descs nor the `entrypoint-backend.sh` systemd claim. This car
  fixes those by hand; nothing gates them afterwards.
* **bare-name model ids**, e.g. flashrank's `ms-marco-MiniLM-L-12-v2` — rule 2's pattern requires a
  slash by construction, and loosening it to bare names would have no closed vocabulary to test
  against.
* **hardcoded ids outside the scan set** — the §5.3 module narrowing, with `embed_service_config.py:151`
  named as the live example the `embed_service/**` clause exists to keep in scope.
* **a field renamed away from the `_MODEL` suffix** escapes rule 1 entirely.
* **host stdio/daemon mode**, where baking is irrelevant — "not baked" is not the same as "not
  usable", which is exactly why the escape hatch is an allowlist-with-rationale rather than a hard
  ban.
* **a wrong claim about a correctly-baked model** — same ceiling `check_registry_prose_liveness.py`
  documents for itself.
* **an allowlist rationale that has gone false.** The stale-entry rule fires when a row's *field*
  changes state (now baked, or deleted). It does **not** re-check the rationale's premise:
  `NLI_MODEL` and `COMET_MODEL` are allowlisted on the grounds that their features are default-OFF,
  and nothing re-fires if `NLI_RERANKING_ENABLED` or `COMET_ENRICHMENT_ENABLED` later flips to
  `True` while the weights stay unbaked. That is the "wrong about a live identifier" ceiling again,
  one level up.
* the **I25 stale-allowlist gap** found at §4.3 step 4. Out of scope here (it is a three-way-sync
  concern, not a model-id one) but it is a genuine hole and the natural composition point with the
  general drift ratchet.

### 5.5 Composition with task #0005

`docs/plans/drift-axis-sweep-2026-06-30.md` is the general ratchet. This guard is **one axis of
it** — *config default ↔ image bake ↔ code literal* — not a competing mechanism. Build it as a
standalone `scripts/check_*.py` in the established idiom so 0005 can absorb it as an axis
implementation rather than having to reconcile a second framework. Say so in the module docstring.

---

## 6. TDD story

CI gates **by directory** (`.forgejo/workflows/ci-pr.yaml:72-83` test-fast;
`:124-128` test-shared; `:176-180` test-backend; `:256-259` test-core). A test in the wrong
directory is never gated in PR CI. `yadgar/tests/integration/` is **not** gated in `ci-pr` at all.

### 6.1 RED first — the guard (§5)

**New:** `yadgar/tests/scripts/test_model_id_liveness.py` — **test-fast** gate.

Write the guard's tests before the guard, and the guard before the §2 edits, so its first run is
against the unfixed tree:

| case | expectation |
|---|---|
| rule 1, unfixed tree | FAILS naming **exactly three** fields — `CROSS_ENCODER_MODEL`, `NLI_MODEL`, `COMET_MODEL` (§5.4). **The RED that proves the guard matches the bug.** Assert the set, not the count |
| rule 1, baked fields | `EMBEDDING_MODEL`, `GTE_RERANKER_MODEL`, `DOC2QUERY_MODEL` pass with no allowlist entry |
| rule 1, empty-default skip | `IMPLICIT_EMBEDDING_MODEL = ""` is skipped, not failed |
| rule 1, org-prefix normalisation | a synthetic `X_MODEL = "some-org/all-MiniLM-L6-v2"` matches a bake of `all-MiniLM-L6-v2` |
| rule 1, allowlist rationale | a row with a <40-char rationale is rejected |
| rule 1, stale allowlist | a row for a now-baked field, and a row for a nonexistent field, are both hard errors |
| rule 2, unfixed tree | FAILS on `local_ml_client.py:202`'s `nli-deberta-v3-small` **plus** the seven `embeddings.py` reference-table hits (3 distinct); passes `local_ml_client.py:137` and `embed_service_config.py:151` because both are in vocabulary |
| rule 2, scan-set FP floor | assert the scan set excludes an HTTP module carrying `"application/json"` — the naive whole-repo variant picks up ~50 such literals and this narrowing is the reason it does not (§5.3) |

Build the fixtures as synthetic temp-dir trees (a fake `config.py` + fake `Dockerfile.backend`),
not by mutating the repo. Then run it once unpatched against the real tree and paste the output
into the PR body — that output *is* the evidence the guard matches the incident.

### 6.2 Existing tests that must change — decide per test, none may be left vacuous

Per §4.2:

* **`yadgar/tests/core/test_backend_observe_p3.py:179`** (test-core) — hard break, remove the
  tuple entry.
* **`yadgar/tests/core/test_idle_eviction_flip.py:13,238,335`** (test-core) — `patch.dict(sys.modules,
  {"flashrank": None})` becomes a no-op. The tests still assert real things about
  `_gte_reranker`/`_cross_encoder` eviction, so **convert**: drop the flashrank key and the
  docstring's `_flashrank_ranker` mention. Do not delete the tests.
* **`yadgar/tests/backend/test_model_load_smoke.py:77-93`** (test-backend) — **delete.**
  `importorskip("flashrank")` means it has never executed in any gate; it pins a dead constant. Its
  `skip_inventory.json:112` note goes in the same commit.
* **`yadgar/tests/core/test_ml_client.py:298`** — unchanged (§2.2 keeps the flag).

### 6.3 GREEN — prove the Ettin path is untouched after editing the fallback chain

The car edits the fallback chain, so the primary must be re-proved, not assumed.

* **Chain-order unit test, mocked** (test-core, extend `test_ml_client.py`): with
  `GTE_RERANKER_ENABLED=True` and a stubbed `_load_gte_reranker`, assert `score_cross_encoder`
  returns the GTE scores and **never** touches `_cross_encoder`. Then with
  `GTE_RERANKER_ENABLED=False`, assert it lands in `_try_st_cross_encoder`. Then with a GTE that
  raises and `GTE_RERANKER_FALLBACK_TO_FLASHRANK=False`, assert zeros. Three branches, all of
  §2.2's true semantics, none previously pinned. **This is the load-bearing regression net for the
  deletion.**
* **Real-weights smoke** (`yadgar/tests/backend/test_model_load_smoke.py`, opt-in via
  `YADGAR_MODEL_LOAD_SMOKE=1`, **not** a CI gate — read its header before touching it):
  `test_ettin_loads_and_scores[cross-encoder/ettin-reranker-32m-v1]` (`:40-56`) is the existing
  proof that the primary loads and scores. Run it locally after the edits. It is untouched by this
  car, which is the point: if it still passes, the chain edit did not perturb the primary. Deleting
  `test_flashrank_loads_and_scores` from the same file does not affect it.
* **Do not** download models in CI or add a new non-opt-in real-weights test (ADR-0032).

### 6.4 Item 2 — what is testable

The comment rewrite has no runtime behaviour. What *is* mechanically checkable, and cheap:
a test in `yadgar/tests/scripts/` (test-fast) asserting that no `ExecStartPre=` directive any
generator emits invokes `cp`. That pins the fact the comment got wrong, in the place a future
generator change would break it. **Keep it to that one assertion**; the general "prose names a real
directive" guard is the one §5.2 rejected on FP grounds, and this narrow version has none.

**Name the two surfaces explicitly, or #0110 kills this test.** Assert over
(a) `yadgar/core/daemon/systemd.py`'s rendered unit text and (b) `flake.nix`'s `ExecStartPre` lists
(`:368-372`, `:429-440`). Both survive #0110 — that car makes `systemd.py` the *sole* renderer, so
the `.in` templates become covered transitively once it lands.

Do **not** reach for `yadgar/tests/_unit_render.py`'s `render_systemd`: it shells out to
`GENERATE_SYSTEMD_SH` (`:26,64`), which is template-sourced, and 0110's plan lists `_unit_render.py`
by name as a Stage-D casualty. A test built on it dies in exactly the way this caveat exists to
prevent.

---

## 7. Verification

**Fully provable locally. No VM, no container, no model download, no infra.**

1. `pytest yadgar/tests/scripts/ yadgar/tests/core/ yadgar/tests/backend/` — the three gates this
   car touches.
2. `python scripts/check_model_id_liveness.py` — clean after §2, and demonstrably RED before it.
3. `python scripts/check_capability_coverage.py` — clean (the §2.6 registry edit is prose-only, so
   it should stay clean; if it fires, a `settings:` line was edited by accident).
4. `python scripts/check_registry_prose_liveness.py` — **run it after §2.1.** Deleting
   `_try_flashrank` kills the last executable `flashrank` identifier, so any registry prose still
   citing it becomes a dead claim. §2.6 fixes `CAPABILITY_REGISTRY.md:278` for this reason; this
   command is the proof.
5. `python scripts/check_skip_inventory.py --validate-inventory` — after the §4.2 step 9 note edit.
6. `python scripts/check_complexity.py` — after the §4.2 step 10 baseline regen.
7. `python scripts/check_backend_bump.py` — after the `server.json` bump. Runs `always_run` anyway.
8. `pre-commit run --all-files` once at the end. **Never `--no-verify`.**
9. Optional, local, opt-in: `YADGAR_MODEL_LOAD_SMOKE=1 uv run --extra test --extra ml pytest
   yadgar/tests/backend/test_model_load_smoke.py -v -n0` (§6.3).
10. Eyeball `~/.config/yadgar/config.yaml` after a regeneration to confirm the corrected
    `FIELD_META` descs land as the on-disk comments. **This is the user-visible deliverable of the
    whole car** — if this step does not change, §2.6 missed the surface that caused the incident.

---

## 8. Rollback

Revert the commits. No state, no migration, no unit regeneration, no image rebuild required for
correctness — the `backend_version` bump means the *next* image build carries the change, but a
running backend is unaffected by any of it (deleting an unreachable branch and rewriting comments
changes no served behaviour).

Two things to un-revert carefully if a partial revert is ever needed: the `.complexity-baseline.json`
regeneration (§4.2 step 10) and the `server.json` bump — both are derived artifacts that must
follow whatever the code ends up being.

---

## 9. ADRs

**New: ADR-0191** — *"flashrank CE tier removed; the ms-marco constants are the degraded ST
fallback, not the reranker."* Small. It must state:

* the decision: delete `_try_flashrank` (provably unreachable — not a dependency, §2.1); keep
  `_try_st_cross_encoder` and `CROSS_ENCODER_MODEL` (reachable, §2.3/§2.4); keep
  `GTE_RERANKER_FALLBACK_TO_FLASHRANK` under its misleading name with corrected prose (§2.2)
* that the CE chain is now **two** tiers, and what each does under which failure
* consequences: the `CROSS_ENCODER_ENABLED` semantic overload (§2.5, flagged not fixed); the
  ST fallback is offline-broken inside the container because its weights are not baked (§2.3); the
  new `check_model_id_liveness` guard and its stated ceiling (§5.4); the I25 stale-allowlist gap
  found at §4.3 step 4
* `revisit_trigger`: (a) `GTE_RERANKER_FALLBACK_TO_FLASHRANK` gets renamed with an alias +
  deprecation window; (b) someone proposes repointing the ST fallback at a baked model (§2.3's
  rejected alternative); (c) an `optimum-onnx` release supporting transformers 5.x reopens the ONNX
  question

**Cross-link, do not supersede:**

* **ADR-0043** (onnx-int8 NO-GO on default) and **ADR-0067** (onnx CE backend removed entirely) —
  0191 is the same reasoning applied to the last remaining ONNX-shaped dead tier. 0067's *"dead
  opt-in = pure maintenance surface"* is the precedent; cite it.
* **ADR-0104** (Ettin swap) — its `GTE_RERANKER_FALLBACK_TO_FLASHRANK` reference now means what
  §2.2 says. Amend + cross-link; no decision is reversed.

**No ADR for work item 2.** Correcting a false comment records no decision. If anything durable is
worth capturing it is the *class* — "comments assert mechanisms nobody checks" — and §6.4's
one-assertion test is the durable artifact for that, not a decision record.

---

## 10. Ordering

**Item 1 is independent of every other car in the train.** It touches no generator, no unit, no
startup path, no storage seam. It does not interact with the `0107 → 0111 → 0113 → 0046` chain,
with `0110` (which follows 0111), or with `0115` (which follows 0027c and 0046).

**Item 2 is likewise independent**, with one deliberate non-dependency: it must **not** wait for
0115 and must **not** name 0115's mechanism (§3.2). 0115's commit adds its own line when it lands.

**One shared, serialised resource: `server.json`'s `backend_version` (§4.1).** Any other car in
`feat/v5.172-bug-train` that touches `yadgar/backend/**`, `entrypoint-backend.sh`, or
`Dockerfile.backend` also bumps it, and two cars claiming the same number is a textual conflict
that `check_backend_bump` will catch in CI but not before. Check the train head at merge time and
sequence the bump; do not pre-allocate a number in the plan.

**One weak coupling worth noting:** §6.4's generator-output assertion must be written against
renderer output rather than `*.in` file text, because #0110 deletes every template. Independent
cars, but 0110 would otherwise turn this test into a Stage-D casualty.

---

## 11. Scope discipline — what this car does NOT do

Found while verifying, deliberately excluded. Listing them so they are not silently absorbed:

* **rename** `GTE_RERANKER_FALLBACK_TO_FLASHRANK` (§2.2) — needs an alias + deprecation window
* **split** `CROSS_ENCODER_ENABLED`'s two meanings (§2.5) — behaviour change on a kill-switch
* **repoint** the ST fallback at a baked model (§2.3) — behaviour change on a failure path, needs a
  quality gate
* **resolve** the NLI `-base`/`-small` licence-table divergence (§2.7) — a licence question; only
  the code literal is fixed here
* **fix** `Dockerfile.backend:67`'s stale `LABEL version="5.0.0"` (§4.1) — unrelated, uncoupled
* **close** the I25 stale-allowlist gap (§4.3 step 4) — a three-way-sync concern; the natural
  composition point with task #0005
* **build** the removed-package prose lint (§5.2) — better as a deps-train companion
