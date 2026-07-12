# Deps-modernization train: transformers 5.x prerequisite for T4 Ettin

**Status: AUDITED — in build (branch `feat/deps-modernization-train`; Q1 decided BLANKET by user 2026-07-12).** Adversarial audit
completed 2026-07-12 against master (core **5.130.0** / backend **5.41.0**). Every pin/version/compat
claim was re-verified against `uv.lock`, `pyproject.toml`, and upstream PyPI metadata (see
§Per-claim verification table). **One material correction landed from the audit: the `hf-xet<1.4`
cap is a HARD blocker to the targeted upgrade** and MUST be raised to `<2.0` in the same Car 1
pyproject edit (transformers 5.x → hub ≥1.3.0 → hf-xet ≥1.5.1; the current cap collides). Two claim
corrections: the forced hub floor is **≥1.3.0 (5.0.0) / ≥1.5.0 (5.13.1)**, not "≥1.0.0" as the draft
stated; the stale-CI-tag surface is **4 workflow files + `Dockerfile.ci-viz`**, not just `ci-pr.yaml`.
A direct embed-drift probe and concrete LongMemEval-baseline mechanics were added. **No fundamental
blocker; the train is GO once the hf-xet cap edit + transformers floor are in the Car 1 pyproject diff.**

**Written 2026-07-12** (working-tree draft; no code changed, no branch, no lock touched). Research
phase: current pins inventoried, transformers 4.57→5.x breaking surface mapped against yadgar's
*actual* usage, sentence-transformers × transformers 5.x compat verified from primary sources, py3.14
wheel availability confirmed, the httpx/starlette landscape re-checked, and the Ettin blocker
**empirically reproduced** on the current pin. Every load-bearing external claim is cited to a primary
source (HF/GitHub/PyPI) in §Research findings. Advisor-reconciled twice (targeted-upgrade + hub
blast-radius + salt-bump + embed-drift guard; then audit reconcile on the hf-xet hard-conflict).

**Audited against:** master (core **5.130.0** / backend **5.41.0**, `33a2f3f4`-descended —
T4 Car 0 #188 has ALREADY LANDED: the `_ckpt` reranker-cache split-brain fix, `CE_SCORING_VERSION`
salt, and query-cache observability are on master. This train builds on that.)

---

## Per-claim verification table (audit 2026-07-12)

Every load-bearing claim re-verified. Method: `uv.lock`/`pyproject.toml` grep for repo facts; PyPI
`/json` metadata for upstream constraints; `git`/branch scan for version-namespace; source grep for
call sites.

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| 1 | Locked versions: transformers 4.57.6, st 5.4.1, hub 0.36.2, tokenizers 0.22.2, optimum 2.1.0, onnxruntime 1.27.0, hf-xet 1.3.2, torch 2.11.0 | **VERIFIED** | `uv.lock` — all eight match the draft table exactly |
| 2 | `pyproject`: `hf-xet<1.4` explicit; transformers transitive via `sentence-transformers[onnx]>=2.2.0`; `requires-python=">=3.14"` | **VERIFIED** | `pyproject.toml:11,160,164` |
| 3 | master = core 5.130.0 / backend 5.41.0; Car 0 #188 (salt+`_ckpt`) landed | **VERIFIED** | `pyproject.toml:7`, `server.json`, `git log` `33a2f3f4 … (#188)` |
| 4 | Version namespace clear (no branch claims 5.131.0/5.132.0/backend 5.42.0/`deps-modern*`) | **VERIFIED** | `git branch -a` + `git ls-remote` — no match |
| 5 | transformers 5.x requires `huggingface_hub>=1.0.0` | **CORRECTED → floor is HIGHER** | PyPI: transformers **5.0.0** → `huggingface-hub>=1.3.0,<2.0`; **5.13.1** → `>=1.5.0,<2.0`. Not "≥1.0". Bigger jump than the draft states. |
| 6 | transformers 5.x pins tokenizers | **VERIFIED (refined)** | PyPI 5.13.1: `tokenizers>=0.22.0,<=0.23.0` — current 0.22.2 is IN range; any bump is capped at 0.23.0 |
| 7 | st `transformers>=4.41.0,<6.0.0` spans 4.x/5.x; st 5.4.1 permits transformers 5.x | **VERIFIED** | PyPI st: `transformers<6.0.0,>=4.41.0` (a WebFetch prose summary wrongly said "no 5.x" — the raw constraint includes 5.x; draft is correct) |
| 8 | optimum 2.1.0 pins `transformers>=4.29` no upper bound | **VERIFIED** | PyPI optimum 2.2.0/2.1.0: `transformers>=4.29`, no ceiling → floats up |
| 9 | `hf-xet<1.4` pin is compatible with hub 1.x (draft frames as "verify") | **CORRECTED → HARD CONFLICT** | PyPI hub 1.23.0: requires `hf-xet>=1.5.1,<2.0` on x86_64/arm64. hub 1.3+ (forced by transformers 5.x) demands hf-xet≥1.5.1; `hf-xet<1.4` collides. **The cap MUST be raised to `<2.0` — mandatory, not contingent.** See Q3. |
| 10 | doc2query uses `AutoModelForSeq2SeqLM`/`AutoTokenizer` (retained 5.x) | **VERIFIED** | `_shared/enrichment/_seq2seq.py:22-26` |
| 11 | CE/NLI + embed go through st wrappers; only doc2query + one e2e `AutoConfig` are direct | **VERIFIED** | `ml_client.py:359-376`, `embeddings.py:131-152`, `test_phase2_subsystems.py` |
| 12 | Env surface clean: `HF_HOME`/`HF_HUB_OFFLINE` used, `TRANSFORMERS_CACHE` unused | **VERIFIED** | `embeddings.py:189`; grep: no `TRANSFORMERS_CACHE` |
| 13 | Zero-warning gate: `filterwarnings=["error"]` + 3 narrow ignores (torch jit, 2× websockets.legacy) | **VERIFIED** | `pyproject.toml` filterwarnings block |
| 14 | CE salt `CE_SCORING_VERSION = "1"` at `embed_service.py`, keyed `f"{model}:{salt}"` | **VERIFIED** | `embed_service.py:206,227` |
| 15 | Stale CI tag `yadgar-ci:5.121.1` in `ci-pr.yaml` | **CORRECTED → WIDER surface** | 13 refs across **4 workflows** (`ci-pr.yaml`×7, `ci-release.yaml`×4, `eval.yaml`×1, `perf.yaml`×1) PLUS `Dockerfile.ci-viz` `FROM`+LABEL. Draft named only `ci-pr.yaml`. |
| 16 | `Dockerfile.ci` frozen-lock bake + `ARG YADGAR_VERSION=5.130.0` + 57-fail comment | **VERIFIED** | `Dockerfile.ci:21,66-72` |
| 17 | `yadgar-ci-viz` inherits base tag → needs coordinated rebuild (Q4) | **VERIFIED (concrete)** | `Dockerfile.ci-viz`: `FROM docker.io/openfantasy/yadgar-ci:5.121.1` + LABEL — hardcodes base; rebuild AFTER yadgar-ci |
| 18 | ADR-0043 made onnx reranking NO-GO → `[onnx]` extra vestigial | **PARTIALLY CORRECT** | ADR-0043 rejected onnx-int8 (2× slower); ADR-0067 "removed onnx backend" — BUT `ml_client.py:375 backend="onnx"` is STILL LIVE (env-gated, `GTE_RERANKER_BACKEND` default "torch"). Extra is dormant, not dead. See Q8. |
| 19 | onnxruntime 1.27.0 ships cp314 wheels (no py3.14 gap) | **TRUSTED (draft live-check)** | Not re-fetched; low-risk, load-smoke (d) would catch a wheel gap. Only load-bearing if `[onnx]` kept (recommended). |
| 20 | httpx 0.28.1 latest stable; 1.0 dev-only; starlette no 1.1+; code uses modern `ASGITransport(app=)` | **TRUSTED (draft) + lock-confirmed** | `uv.lock`: httpx 0.28.1, starlette 1.0.0. Migration correctly deferred. |
| 21 | ci-release auto-builds core+backend on version-bump merge (build-minutes note) | **VERIFIED (audit addition)** | `ci-release.yaml:153-246`: builds `Dockerfile`(core)+`Dockerfile.backend` on merge when pyproject≠latest tag. Does NOT build yadgar-ci. Baseline for any bump — noted so the DockerHub push is expected, not a surprise. |
| 22 | Offline TEST legs survive hub 1.x error-type changes on offline miss | **VERIFIED SAFE** | No test asserts specific HF exception types/messages on offline miss; offline legs set `HF_HUB_OFFLINE=1` env only. Prod `embeddings.py:_load_sentence_transformer` catches bare `except Exception` — insensitive to hub error-type churn. |

**Audit-net:** 2 corrections that change scope (hf-xet hard conflict #9; wider stale-tag surface #15),
2 claim-precision fixes (hub floor #5; onnx path live #18), rest verified/trusted. **No claim
invalidates the train's premise or GO status** — the corrections tighten Car 1's pyproject edit and
Car 2's file list.

---

**Why this train exists:** the T4 Ettin train (`docs/plans/t4-ettin-train-2026-07-12.md`) is
BLOCKED. `cross-encoder/ettin-reranker-{32m,68m}-v1` cannot load on yadgar's current transformers
pin — the models declare `tokenizer_class: "TokenizersBackend"` and `transformers_version: "5.7.0"`
in their published configs, and `TokenizersBackend` does not exist in transformers 4.x. This train
upgrades the pins so Ettin *can* load; **T4 then resumes unchanged** and does the actual model swap
+ eval. This train changes **no reranker default** — it only proves the new stack loads and scores
Ettin (a smoke gate), and keeps GTE as prod default.

**Folds in pending task #35** (starlette 1.1+ / httpx2 migration + `uv lock --upgrade`) — but the
research materially rescopes #35: see §The #35 rescope. Short version: **no httpx/starlette major
migration happens in this train** (the target releases do not exist stable as of July 2026), and the
`uv lock --upgrade` is deliberately **targeted, not blanket**, to avoid re-triggering the documented
57-fail incident.

---

## BLUF — what this train actually is

Upgrade the HuggingFace ML stack — `transformers` (4.57.6 → 5.x), and the deps that move with it
(`sentence-transformers`, `huggingface-hub`, `tokenizers`) — via a **targeted** `uv lock --upgrade`
of exactly those packages, then triage every new deprecation warning under the permanent
`filterwarnings=["error"]` zero-warning gate, rebuild the frozen-lock CI image, and prove the new
stack loads + scores Ettin and still loads the current prod models (GTE, embed, flashrank, doc2query).

**The core move is one re-lock + a warning-triage pass.** yadgar touches `transformers` almost
entirely through `sentence-transformers` wrappers (`CrossEncoder`, `SentenceTransformer`); the only
direct `transformers` API calls are doc2query's `AutoModelForSeq2SeqLM`/`AutoTokenizer` and a single
e2e `AutoConfig.from_pretrained` — all stable across the 5.x bump. So the *code*-migration surface is
tiny. **The risk is not the code; it is the transitive deprecation blast radius under the
zero-warning gate** — chiefly the forced `huggingface_hub` major bump (0.36.2 → **≥1.3.0/≥1.5.0**,
PyPI-verified — not "≥1.0"), which carries its own deprecation surface AND **forces a mandatory
`hf-xet<1.4`→`<2.0` cap raise** (hub 1.x requires hf-xet≥1.5.1 — the current cap is a hard resolve
blocker; audit Q3).

**Acceptance is dominated by two gates:** (a) the full suite green under `filterwarnings=["error"]`
with every *new* third-party deprecation triaged (pinned message-specific ignore, or fixed —
first-party warnings get FIXED, never ignored); and (b) the **Ettin load gate** — the reason this
train exists — `CrossEncoder("cross-encoder/ettin-reranker-32m-v1")` loads and scores a smoke pair
on the new pins. Plus a **LongMemEval sanity arm** (GTE-on-old-stack vs GTE-on-new-stack recall@k
parity) to catch silent scoring/embedding drift the bump could introduce with model ids unchanged.

**Structure:** ONE PR (per the ADR-0088 convention: train = one PR, one version; a new deps ADR at
ADR-0099+ records this train's own decisions). Car 1 = the lock upgrade + code
migrations + warning triage + CE salt bump. Car 2 = CI frozen-lock image rebuild/tag + the load/eval
gates. See §Car breakdown.

**Version:** this train takes **core 5.131.0 / backend 5.42.0** (namespace verified clear). T4's
train body re-claims the next slot after (T4's own audit renumbers it — this plan does NOT edit T4's
version tables).

---

## The blocker — empirically reproduced (premise iron-clad)

Attempting `CrossEncoder("cross-encoder/ettin-reranker-32m-v1")` on the current pin
(transformers 4.57.6, sentence-transformers 5.4.1) fails with **two distinct errors**:

1. `AutoTokenizer.from_pretrained` → `ValueError: Tokenizer class TokenizersBackend does not exist
   or is not currently imported.` — `tokenizer_config.json` sets `"tokenizer_class":
   "TokenizersBackend"`, a class introduced in the transformers 5.x tokenizer-backend refactor;
   absent from 4.x's tokenizer mapping.
2. `sentence_transformers` then calls `AutoProcessor.from_pretrained` →
   `ValueError: Unrecognized processing class in cross-encoder/ettin-reranker-32m-v1.` — the
   AutoProcessor modality routing the Ettin CrossEncoder uses is a 5.x capability.

`trust_remote_code=True` does not bypass this — the failure is in HF auto-class *resolution*, not
remote-code execution. The models' `config.json` declares `"transformers_version": "5.7.0"`; the
model cards list framework = Transformers 5.7.0 / Sentence Transformers 5.4.1 / **Tokenizers 0.22.2**
(the last is exactly yadgar's current tokenizers lock — a reassuring alignment). **There is no
workaround on 4.57.6.** transformers ≥5.0 is a hard requirement for T4.

---

## Research findings (primary-source, cited)

### Current pins (from `pyproject.toml` + `uv.lock`, master 5.130.0)

`requires-python = ">=3.14"`. **transformers is NOT directly pinned** in `pyproject.toml` — it
resolves transitively through `sentence-transformers[onnx]` (floor `sentence-transformers>=2.2.0`).
There is **no deliberate transformers version-hold rationale** anywhere in the repo (no pyproject
comment, no CHANGELOG note). The 4.57.6 lock is simply what the last full resolve produced.

| Dep | pyproject constraint | uv.lock resolved | Notes |
|---|---|---|---|
| transformers | *(transitive via sentence-transformers[onnx])* | **4.57.6** | no direct pin; no hold rationale |
| sentence-transformers | `>=2.2.0` (ml extra) | **5.4.1** | already spans transformers 4.x+5.x |
| torch | *(transitive via st)* | **2.11.0** | py3.14 TorchScript DeprecationWarning already gate-exempted |
| tokenizers | *(transitive)* | **0.22.2** | matches Ettin model-card framework version |
| optimum | *(via `sentence-transformers[onnx]`)* | **2.1.0** | pins `transformers>=4.29` **no upper bound** (published 2025-12-19, post-5.0) → does NOT block the transformers-5.x float; moves with the HF stack |
| onnxruntime | *(via `[onnx]` extra)* | **1.27.0** | ships **cp314 wheels** (linux x86_64, requires-python ≥3.11) → no py3.14 gap; onnx 1.22.0 alongside |
| huggingface-hub | *(transitive)* | **0.36.2** | **5.x forces ≥1.3.0 (5.0.0) / ≥1.5.0 (5.13.1) — the major bump** (PyPI-verified; NOT ≥1.0) |
| hf-xet | `<1.4` (explicit) | 1.3.2 | **CAP MUST RISE to `<2.0`** — hub 1.x requires `hf-xet>=1.5.1`; `<1.4` collides (audit correction, Q3) |
| numpy | `>=1.24.0` | 2.4.4 | |
| scipy | `>=1.11.0` | 1.17.1 | |
| fastapi | `>=0.109.1` | 0.136.1 | **HELD — not upgraded this train** |
| sse-starlette | `>=1.6.0` | 3.3.4 | **HELD** |
| starlette | *(transitive via sse-starlette/fastapi)* | **1.0.0** | **HELD** (already at 1.0.0; see #35 rescope) |
| uvicorn | `>=0.24.0` | 0.46.0 | **HELD**; websockets.legacy DeprecationWarning already gate-exempted |
| httpx | `>=0.27` | **0.28.1** | **HELD**; code already uses modern `ASGITransport(app=)` |
| pydantic | `>=2.5.0` | 2.13.3 | **HELD** |
| mcp | `>=1.23.0` | 1.27.0 | **HELD** |

### transformers 4.57 → 5.x breaking surface × yadgar's ACTUAL usage

transformers 5.0.0 shipped December 2025; latest is **5.13.1** (July 2026). Minimum Python ≥3.10
(supported through 3.14); minimum torch ≥2.4 formal (≥2.6 effective for non-safetensor loads — Ettin
uses safetensors, so moot). The published `MIGRATION_GUIDE_V5.md` lists the breaking surface. Mapped
against every direct transformers/st call site in yadgar:

| yadgar call site | file:line | API used | 5.x verdict |
|---|---|---|---|
| doc2query loader | `yadgar/_shared/enrichment/_seq2seq.py:22-26` | `AutoModelForSeq2SeqLM.from_pretrained`, `AutoTokenizer.from_pretrained`, `.to(device)`, `.eval()` | **SAFE** — these classes/APIs are retained in 5.x (only `AutoModelWithLMHead` and `AutoModelForVision2Seq` were removed/renamed; neither used here) |
| embed model | `yadgar/_shared/embeddings/embeddings.py:131-152` | `SentenceTransformer(..., trust_remote_code=True, local_files_only=...)`, `.encode()`, `.get_sentence_embedding_dimension()` | **SAFE (st-wrapped)** — st absorbs the transformers bump; watch `.encode()` return-type nuances in triage |
| CE reranker + NLI | `yadgar/backend/ml_client/ml_client.py:359-376` | `from sentence_transformers import CrossEncoder as STCrossEncoder`; `STCrossEncoder(model, max_length=, backend=, model_kwargs=)`, `.predict(pairs)` | **SAFE (st-wrapped)** — CrossEncoder modular arch (st 5.4.0+) is what Ettin needs; API stable |
| e2e cache check | `yadgar/tests/e2e/test_phase2_subsystems.py:832-836` | `AutoConfig.from_pretrained(model, local_files_only=True)` | **SAFE** — AutoConfig retained |
| benchmark (test-only) | `benchmarks/run_locomo_jscore.py:255-275` | `AutoModelForCausalLM`, `AutoTokenizer(trust_remote_code=)`, `BitsAndBytesConfig(load_in_4bit=True)`, `.generate(...)`, `apply_chat_template` | **WATCH (test-only, not prod)** — `BitsAndBytesConfig(load_in_4bit=True)` field is fine (only the `from_pretrained(load_in_4bit=)` *shortcut kwarg* was removed); `apply_chat_template` now returns `BatchEncoding` not bare `input_ids` — this benchmark may need a small fix if it indexes the result. Non-blocking for the train (benchmark, not shipped path). |

**No code hardcodes a `tokenizer_class` or tokenizer/model class-name string** (grep-confirmed) — so
nothing in yadgar's code assumes the old tokenizer file layout.

**Env-var surface (verified clean):** transformers 5.x removes `TRANSFORMERS_CACHE` (→ `HF_HOME`).
yadgar uses **`HF_HOME` and `HF_HUB_OFFLINE`** (`embeddings.py:89,189-199`; CI `ci-pr.yaml`; tests) —
**both retained in 5.x**. `TRANSFORMERS_CACHE` is used **nowhere**. No env migration needed.

**Other 5.x removals — none used by yadgar:** removed pipelines (`question-answering`, `summarization`,
`translation`, …) — yadgar uses no `pipeline()`; Flax/TF sunset — yadgar is torch-only; `from_pretrained`
from URL removed — yadgar uses Hub IDs / local paths; `safe_serialization=False` removed / safetensors
mandatory — yadgar loads safetensors models. All clear.

**The one forced transitive major bump — `huggingface_hub` (floor ≥1.3.0, likely ≥1.5.0):**
transformers 5.x switched its HTTP backend from `requests` to `httpx` and raises the hub floor.
**PyPI-verified (audit):** transformers **5.0.0** pins `huggingface-hub>=1.3.0,<2.0`; transformers
**5.13.1** pins `>=1.5.0,<2.0`. (The draft said "≥1.0.0" — the real floor is higher; the jump from
yadgar's locked 0.36.2 is even larger than first stated.) This is a **major-version jump** with its
own deprecation surface **and it forces a hard edit to the `hf-xet<1.4` pin** — see the next paragraph
and Q3. **Under the zero-warning gate this is the single most likely source of new fatal
deprecations.** Car 1 must triage hub's deprecation output explicitly. This is the riskiest research
item; see §Risks.

**Forced pyproject edit — `hf-xet<1.4` → `<2.0` (MANDATORY, audit correction):** the current
`hf-xet<1.4` cap exists because *old* hub 0.36 still calls `download_files()`, which hf-xet 1.4
deprecates → fatal under the gate (per the pin comment). **But hub 1.x REQUIRES the newer hf-xet:**
PyPI hub 1.23.0 pins `hf-xet>=1.5.1,<2.0` on x86_64/arm64, and transformers 5.x forces hub ≥1.3.0.
So `hf-xet<1.4` **directly collides** with the modern-hub resolution the train needs: either the
targeted resolve fails outright, or uv back-solves onto an ancient hub that reintroduces the very
`download_files()` deprecation the pin was guarding against → fatal under the gate. **Either way the
cap MUST be raised** — to `<2.0` (mirror hub's own ceiling), in the SAME Car 1 pyproject edit as the
`transformers>=5.0` floor add. The raise is not just forced but *clean*: at hub ≥1.5 the pin's stated
reason (old-hub `download_files()`) dissolves — hub adopting XetSession is exactly the "upstream
tracking required" the comment was waiting on. **Audit closes Q3: the pin edit is mandatory and safe.**

### sentence-transformers × transformers 5.x — already compatible

sentence-transformers pins `transformers>=4.41.0,<6.0.0` (wide, deliberate — dual 4.x/5.x CI since
st 5.2.1; "v5.4.0 onwards supports both Transformers v4 and v5"). **yadgar's current lock, st 5.4.1,
ALREADY permits transformers 5.x.** So the re-lock can float transformers up without touching the st
floor. The Ettin CrossEncoder modular arch landed in st 5.4.0 — 5.4.1 is sufficient; no st API change
breaks `CrossEncoder(...)`. (Latest st is 5.6.0; a bump to it is optional, not required — recommend
holding st at the resolver's choice unless triage forces it.)

**The `[onnx]` extra sibling deps do NOT block (pre-flighted).** yadgar requests
`sentence-transformers[onnx]>=2.2.0`, which pulls `optimum` + `onnxruntime`. A blanket concern was
that optimum could cap `transformers<5` and break the targeted resolve on the user's machine (past
all this train's gates). Verified from PyPI: **optimum 2.1.0** (locked; published 2025-12-19, after
transformers 5.0) pins `transformers>=4.29` with **no upper bound** → floats up cleanly. **onnxruntime
1.27.0** (locked) ships **cp314 wheels** → no py3.14 gap. So the targeted `uv lock --upgrade` of the
HF stack (which drags optimum/onnxruntime/onnx along in the same extra) resolves. **Note:** ADR-0043
already declared onnx reranking NO-GO (thread-thrash), so the `[onnx]` extra is arguably vestigial —
an audit could consider dropping it, but it is NOT a blocker either way.

### torch × py3.14 wheels — no blocker

torch ≥2.4 satisfied (yadgar at 2.11.0; latest 2.13.0). Python-3.14 wheels: **tokenizers** (the only
compiled dep that matters) ships `cp310-abi3` wheels covering 3.14 with no cp314-specific build;
**torch** has cp314 **CPU** wheels (CUDA cp314 only on Linux/CUDA-13; yadgar's backend is CPU
inference → fine); **transformers/sentence-transformers** are pure-Python. **No py3.14 wheel-gap risk
for this train.** (GPU-on-3.14 is a general deployment caveat, irrelevant to yadgar's CPU path.)

### The #35 rescope — httpx/starlette

**httpx 1.0 / "httpx2" is NOT released** (July 2026): latest stable is 0.28.1 (Dec 2024); only
`1.0.dev1–dev3` pre-releases exist; the encode/httpx maintainer **closed all issues/discussions
Feb 2026**. **starlette** is at 1.0rc1/1.0.0 — **no 1.1+**. And yadgar's httpx code **already uses the
modern `transport=httpx.ASGITransport(app=...)` pattern** — the `AsyncClient(app=...)` removal bit at
httpx 0.28.0, which the repo already survived; grep found **zero** deprecated `AsyncClient(app=)` /
`Client(app=)` sites in prod or tests. **Therefore this train performs no httpx/starlette migration.**
What #35 genuinely contributes is the re-lock — which the transformers blocker forces regardless.

**Recommendation (task-vs-research tension, flagged for audit):** the task said "`uv lock --upgrade`"
(blanket). A blanket re-lock could pull a newer starlette/httpx and **re-trigger the exact 57-fail
incident** the lock-parity bake was installed to prevent (documented at `Dockerfile.ci:64-70`,
2026-07-10: an unpinned resolve pulled `starlette>1.0` whose TestClient deprecates plain httpx →
fatal under `filterwarnings=error` → 57 CI failures). **Recommend a TARGETED upgrade** —
`uv lock --upgrade-package transformers --upgrade-package sentence-transformers
--upgrade-package huggingface-hub --upgrade-package tokenizers` (float the HF stack together) — and
**HOLD** starlette/httpx/fastapi/uvicorn/pydantic/mcp at current pins. **Note (audit):** `hf-xet` is
transitive *under* hub, not in the `--upgrade-package` set — but hub 1.x drags hf-xet ≥1.5.1, which
the `hf-xet<1.4` cap forbids. The pyproject cap-raise (`<1.4`→`<2.0`) is what lets this command
resolve at all; make the pyproject edit BEFORE running the lock command. This serves the required goal
(transformers 5.x for Ettin) with minimal blast radius and directly avoids the known failure mode.
The true starlette/httpx *major* migration is **deferred to when those releases land stable** (a
future #35 successor). *Audit decision: confirm targeted-vs-blanket.*

**Also recommend:** add an explicit `transformers>=5.0` floor to `pyproject.toml` (currently
transitive). The Ettin requirement is now load-bearing; without a floor, a future re-lock could
silently drop transformers back under st's wide `<6.0.0` bound and re-break Ettin. Document the floor
with a rationale comment (mirrors the existing hf-xet pin-comment convention).

### CI lock-parity mechanism (the thing a deps change forces to rebuild)

`Dockerfile.ci` (base `python:3.14-slim`, `ARG YADGAR_VERSION=5.130.0`) bakes deps via
`uv export --frozen --no-emit-project --extra test --extra ml -o requirements-lock.txt` then
`uv pip install --system -r …` (`Dockerfile.ci:71-76`). CI jobs then run
`uv pip install --system --no-deps -e .` — installing only the yadgar package, **never re-resolving**.
This is the lock-parity wall: CI runs the exact versions `uv.lock` pins, and cannot drift. **A lock
change ⇒ the frozen bake changes ⇒ the CI image MUST be rebuilt and re-tagged**, else CI silently
tests the old deps. The comment at `Dockerfile.ci:64-70` states this is load-bearing (the 57-fail
incident). **Stale-tag hazard:** `.forgejo/workflows/ci-pr.yaml` still references
`docker.io/openfantasy/yadgar-ci:5.121.1` (lines 44/94/145/210/…) — 9 versions behind pyproject's
5.130.0 — and yadgar-ci has **no auto-sync pipeline** (tag is manual). This train's Car 2 must bump
that tag reference in lockstep with the rebuild.

### The zero-warning gate

`pyproject.toml:221-240`: `filterwarnings = ["error", …]` — **all warnings are test failures,
permanently** (ADR-0087). Only three narrow, message-pinned third-party ignores exist today: torch
`torch.jit.script_method` py3.14, and two uvicorn `websockets.legacy` aliases. **This gate is why a
deps bump is dangerous:** any *new* deprecation from any bumped package (esp. huggingface_hub 1.0) is
fatal. The gate's own rule: first-party warnings get FIXED; third-party get a *narrow,
message-specific, revisit-noted* ignore — never a bare category ignore.

---

## Acceptance gates (the train's exit criteria)

**(a) Full suite green under the zero-warning gate.** All CI legs pass with
`filterwarnings=["error"]`. **Every new third-party deprecation introduced by the bump is triaged**:
either fixed (first-party) or given a narrow message-pinned ignore with a revisit note (third-party),
appended to the `filterwarnings` block in the existing style. No bare-category ignores. Expect the
bulk of new warnings from `huggingface_hub` 1.0 and possibly `tokenizers`.

**(b) Ettin load gate — THE gate this train exists for.** On the new pins,
`CrossEncoder("cross-encoder/ettin-reranker-32m-v1")` loads AND scores a smoke pair
(`.predict([("hello world","a greeting")])` returns a finite float). Same for `-68m-v1` (the T4
fallback). This is the smoke proof that the pin upgrade unblocks T4. **No model default is changed** —
this is a load smoke, not a swap.

**(c) GTE reranker still loads** (prod CE model until T4):
`CrossEncoder("Alibaba-NLP/gte-reranker-modernbert-base")` loads + scores on the new stack. The prod
reranker must not regress on the upgrade.

**(d) Embed + flashrank + doc2query load smoke:** `all-MiniLM-L6-v2` (`/embed`),
`ms-marco-MiniLM-L-12-v2` (flashrank fallback), `doc2query/msmarco-t5-small-v1`
(`AutoModelForSeq2SeqLM`) each load and produce output on the new stack. Covers every default-ON
prod model path against the transformers/st bump.

**(e) LongMemEval short sanity run — silent-drift catch (define arm + criterion).** The
transformers/tokenizers bump can shift scores **with model ids unchanged**: (i) GTE CE scores shift
because the tokenizer changes numerically; (ii) `all-MiniLM-L6-v2` embeddings can shift, and stored DB
embeddings were computed on the OLD stack — no cache salt fixes an embed-space shift.
- **Arm definition:** `GTE-on-old-stack` vs `GTE-on-new-stack` — identical model ids
  (`GTE_RERANKER_MODEL` = GTE, embed = MiniLM), the ONLY variable is the dep stack (old lock vs new
  lock). Reuse the T4/T3 `make longmemeval Q=<small>` harness (in-process `--unified
  --retrieval-only`, all-6-types explicit `--types` to avoid the single-type collapse trap, NullCache
  per-arm so no cross-arm cache bleed). Small Q (e.g. 10/type) is enough — this is a drift sanity
  check, not a model gate.
- **Baseline mechanics (audit — a parity gate must be able to produce its baseline):** the
  OLD-stack (`GTE-on-old-stack`) numbers are produced by running the harness **on master, BEFORE the
  branch flips the lock** — i.e. capture the old-lock recall@k table first (checkout master / the
  pre-lock commit, run the arm, save the table into this plan), THEN run the new-lock arm on the
  branch and diff. The old arm is NOT recomputable after the lock changes (the whole point is the
  stack differs) — so it MUST be captured up-front. Do not defer the baseline; a gate whose baseline
  can't be produced is theater.
- **Criterion:** recall@{5,10} + MRR **parity within the determinism noise band** (run one arm twice
  first to fix the band; head-slice question selection is deterministic, so the band should be ~0).
  Pass → the bump is scoring-neutral, ship. Drift beyond the band → **investigate before proceeding**:
  a CE-score shift is covered by the salt bump below; an *embedding-space* shift may require a
  re-embed of the store (flag to the user — do NOT silently ship a stack that silently re-ranks).

**(e) RESULTS (build, 2026-07-12) — PASS.** Arm mechanics as specified with one deviation: the
harness ran the **legacy in-process retrieval path, NOT `--unified`** — post-ADR-0078 the unified
path is a thin forwarder that REQUIRES a live backend (`YADGAR_EMBED_URL`); with none it zero-scores
(observed: an initial `--unified` run returned all-zero metrics with instant `_forward_to_backend`
errors). The T3 Car 1 arm precedent (`lme_t3car1_arm_*.json`) is likewise `unified_recall: false`.
Both arms identical flags: `--retrieval-only --variant s --stratify-per-type --max-questions 30`
+ all six explicit `--types`. Old-stack arm run TWICE on master pre-flip for the determinism band.

| arm (Q=30, 6 types) | recall@5 | recall@10 | MRR |
|---|---|---|---|
| GTE-on-old-stack run 1 (master, transformers 4.57.6) | 0.9139 | 0.9500 | 0.9708 |
| GTE-on-old-stack run 2 (determinism band) | 0.9306 | 0.9667 | 0.9708 |
| **GTE-on-new-stack** (transformers 5.13.1) | **0.9500** | **0.9750** | **0.9714** |

Band (old run1↔run2): ±0.017 recall@k, 0.000 MRR — per-question retrieval is NOT perfectly
deterministic (multi-session is the noisy type: 0.683/0.633/0.750 across the three runs).
New-arm deltas: +0.019 recall@5 vs old-run2 (marginally past the 2-run band, ~1 question,
**favorable direction**), +0.008 recall@10, +0.0006 MRR. Verdict: parity within noise — no
degradation, and the direct probe (f) shows the embed space and CE scores are EXACT, so the
recall@5 fluctuation is run-to-run nondeterminism, not stack drift. Raw reports:
`benchmarks/reports/deps-train-lme-gte-{oldstack-run1,oldstack-run2,newstack}.json`.

**(f) Direct embed-drift probe — cheap, deterministic, in-train (audit addition).** Because st 5.4.1
is HELD and the embed model id (`all-MiniLM-L6-v2`) is unchanged, an embedding-space shift can only
enter via transformers-5.x tokenizer changes propagating through the st wrapper — plausible but not
proven. Rather than rely on arm (e)'s *indirect* recall@k signal, add a **direct** probe: embed a
fixed sentence (e.g. `"the quick brown fox"`) on the OLD stack and the NEW stack, assert
`cosine(old, new) ≥ 0.9999`. This is one embed call per stack — cheaper and unambiguous vs a
LongMemEval run, and it isolates embed drift from CE drift. **Capture the old-stack vector on master
before the lock flip** (same discipline as (e)'s baseline). Cosine < threshold → embedding-space
drift is real → escalate the re-embed decision to the user BEFORE shipping (do not silently ship a
stack that re-embeds the store's semantic space). Pass → embed space is stable; arm (e) then only has
to clear CE-score drift (covered by the salt bump). This probe is the primary embed-drift catch; (e)
is the corroborating end-to-end check.

**(f) RESULTS (build, 2026-07-12) — PASS, exact.** Old-stack vectors captured on master pre-flip
(`benchmarks/reports/deps-train-embed-drift-baseline-oldstack.json`: 4 fixed sentences via
`all-MiniLM-L6-v2` with prod kwargs, + 2 GTE CE pairs informational). New stack
(transformers 5.13.1 / hub 1.23.0 / torch 2.13.0, st + tokenizers HELD):
**cosine = 1.00000000 on all four sentences** — the embed space is byte-stable, not merely
within threshold. Bonus: the GTE CE scores on the fixed pairs are IDENTICAL to 6 decimals
(delta +0.000000) — the feared tokenization-driven CE shift did not materialize on these pairs;
the salt bump below stays as defense-in-depth for the persistent cache. No re-embed decision
needed.

**CE_SCORING_VERSION salt bump — `"1" → "2"` (recommended, justified):** the disk-persistent CE score
cache keys on `f"{reranker_model_id}:{CE_SCORING_VERSION}"` (`embed_service.py:227`, Car 0 #188). The
transformers-5.x TokenizersBackend changes tokenization → GTE CE scores shift **numerically with the
model id unchanged** → the persistent cache would serve **stale pre-upgrade GTE scores** under the new
stack. Bumping the salt to `"2"` forces the existing discard-on-mismatch path to drop the old snapshot
on load. This is **this train's own scope** (a scoring-semantics change from the dep bump), independent
of T4's later model-id swap. One-line edit + the existing Car 0 salt test (`monkeypatch` of
`CE_SCORING_VERSION`) already proves the mechanism.

---

## Car breakdown (ONE PR, ADR-0088)

### Car 1 — targeted lock upgrade + code migrations + warning triage + salt bump

**Scope (the dep + code body):**
- **Targeted `uv lock --upgrade`** of `{transformers, sentence-transformers, huggingface-hub,
  tokenizers}` only (hold everything else — §The #35 rescope). Prepared as a lock diff; **the plan
  hands the exact `uv lock` command to the user via `MIGRATION_NOTES.md` — the agent does NOT run it
  or stage the lock** (No-Auto-Apply rule; a lock re-resolve is an infra mutation). Resulting
  `uv.lock` + `pyproject.toml` floor edit are reviewed as a normal diff.
- **Add `transformers>=5.0` floor** to `pyproject.toml` with a rationale comment (Ettin requirement,
  mirrors hf-xet pin-comment style), **AND raise the `hf-xet<1.4` cap to `<2.0`** in the SAME edit —
  this is **mandatory, not contingent** (audit Q3): hub 1.x requires `hf-xet>=1.5.1`, so the old cap
  blocks the resolve. Update the pin's rationale comment (old-hub `download_files()` reason dissolved
  at hub ≥1.5; cap now just tracks hub's own `<2.0` ceiling).
- **Same-commit discipline:** the lock diff, the `pyproject` floor+cap edits, AND the salt bump land in
  **one commit** — zero stale-score window (the salt must not lag the lock, else the persistent CE
  cache serves pre-upgrade scores between commits).
- **Warning-triage pass** under `filterwarnings=["error"]`: run the full suite, enumerate every NEW
  deprecation, fix first-party, add narrow message-pinned ignores (with revisit notes) for
  third-party. Expect huggingface_hub 1.x (≥1.3.0/≥1.5.0) to dominate.
- **Code migrations (small, if any surface in triage):** the audited surface is stable (doc2query
  AutoModel/AutoTokenizer + e2e AutoConfig retained). The only *possible* touch is the test-only
  benchmark `apply_chat_template` return-type (`BatchEncoding` vs bare ids) — fix only if the
  benchmark leg exercises it; non-blocking for the shipped path.
- **CE salt bump `CE_SCORING_VERSION "1"→"2"`** (`embed_service.py`) — GTE tokenization drift busts
  the persistent CE cache. This touches `backend/` → `check_backend_bump.py` fires → backend_version
  bumps naturally.

**Acceptance:**
- Full suite green under the zero-warning gate; every new warning triaged (fixed or narrowly ignored
  with a revisit note); no bare-category ignores.
- `transformers>=5.0` floor present AND `hf-xet<2.0` cap present (both in the same pyproject edit);
  lock resolves transformers to ≥5.0, hub to ≥1.3.0, hf-xet to ≥1.5.1 (verify the resolved versions
  in the lock diff); st/tokenizers moved consistently.
- Ettin load gate (b), GTE load gate (c), embed/flashrank/doc2query smoke (d) all PASS on the new
  lock — run these as real load+score smokes, NOT mocked (the CI suite mocks CE per-test; these
  smokes must load real models, so run them locally / in a dedicated non-mocked leg).
- Salt bumped; the Car 0 salt test still green.

**Test plan:** the warning-triage IS the gate for (a). Add a load-smoke test module that loads Ettin-32m,
Ettin-68m, GTE, MiniLM-embed, doc2query on the real stack and asserts each scores/encodes (marked to
run outside the CE-mocked CI legs, or as a local pre-merge gate — mirror how T4 plans its non-mocked
load checks). Run the LongMemEval drift arm (e) and paste the recall@k parity table into this plan.

**Model label:** sonnet builds + runs; **opus for the warning-triage ignore-vs-fix judgments** and the
(e) drift go/no-go (both are correctness-authorising judgments, not mechanical pass/fail).

### Car 2 — CI frozen-lock image rebuild + tag + gates hand-off

**Scope (the CI-image reconciliation the lock change forces):**
- The `uv.lock` change ⇒ `Dockerfile.ci`'s `uv export --frozen` bake now produces different pinned
  versions ⇒ **the yadgar-ci image must be rebuilt and re-tagged**, and **ALL** stale
  `yadgar-ci:5.121.1` references bumped in lockstep. **Audit-corrected surface — 13 refs across 4
  workflow files, NOT just `ci-pr.yaml`:**
  - `.forgejo/workflows/ci-pr.yaml` — 7 refs (`yadgar-ci` + `yadgar-ci-viz`)
  - `.forgejo/workflows/ci-release.yaml` — 4 refs
  - `.forgejo/workflows/eval.yaml` — 1 ref
  - `.forgejo/workflows/perf.yaml` — 1 ref
  - `Dockerfile.ci-viz` — `FROM docker.io/openfantasy/yadgar-ci:5.121.1` + LABEL (the viz base tag)

  Update `Dockerfile.ci`'s `ARG YADGAR_VERSION` to the train version. The `ci-release.yaml`/`eval.yaml`/
  `perf.yaml` tag edits are the same mechanical file edits as `ci-pr.yaml`.
- **`yadgar-ci-viz` (Q4 — answered concrete):** `Dockerfile.ci-viz` does `FROM yadgar-ci:5.121.1` —
  it hardcodes the base tag, so it MUST be rebuilt AFTER `yadgar-ci` (dependency chain) with both its
  `FROM` and LABEL bumped. Not "inherits automatically" — an explicit coordinated rebuild. Put both
  build commands (yadgar-ci first, then yadgar-ci-viz) in `MIGRATION_NOTES.md`.
- **ci-release core+backend auto-build note (audit — expected, not a defect):** merging this
  version-bumped PR triggers `ci-release.yaml`'s `build-images` job, which builds+pushes the **core**
  (`Dockerfile`) and **backend** (`Dockerfile.backend`) DockerHub images on merge (fires when pyproject
  ≠ latest `v*` tag; `ci-release.yaml:153-246`). This is baseline for ANY version bump — flag it in
  MIGRATION_NOTES so the DockerHub push is expected. **ci-release does NOT build `yadgar-ci`** — that
  image has no auto-build pipeline and stays a manual user step (below).
- **No-Auto-Apply / build-minutes discipline:** the plan **prepares** the exact build + push commands
  and hands them to the user via `MIGRATION_NOTES.md`; the agent does NOT build or push the image.
  (The workflow rule: images build/push under the user's control; the agent stages the Dockerfile +
  workflow-tag edits only.) The `ci-pr.yaml` tag-reference edit IS a normal file edit the agent makes;
  the *build+push* is the user's step.
- Estimate: one `yadgar-ci` rebuild (frozen bake picks up the new HF stack — larger transformers 5.x /
  hub 1.x wheels; image grows modestly) THEN one `yadgar-ci-viz` rebuild (it `FROM`s the new
  yadgar-ci tag — confirmed hardcoded, must follow). Two coordinated builds, both in MIGRATION_NOTES.

**Acceptance:**
- `Dockerfile.ci` `ARG` version + ALL `yadgar-ci`/`yadgar-ci-viz` tag references across the 4
  workflows (`ci-pr.yaml`, `ci-release.yaml`, `eval.yaml`, `perf.yaml`) + `Dockerfile.ci-viz`
  (`FROM`+LABEL) point at the new train tag. **Grep-verified: zero `5.121.1` residue anywhere under
  `.forgejo/` or `Dockerfile.ci-viz`** (`grep -rn 5.121.1 .forgejo/ Dockerfile.ci-viz` returns
  nothing).
- MIGRATION_NOTES carries the exact `docker build … && docker push …` command(s) for BOTH images
  (yadgar-ci then yadgar-ci-viz, in order) for the user to run, plus the frozen-lock re-export note
  and the "core+backend auto-build on merge is expected" note.
- After the user rebuilds+pushes and CI runs on the new image: all legs green (this is the real
  integration proof that the frozen bake carries the new stack and the zero-warning gate holds in CI,
  not just locally — the 57-fail incident was a CI-only divergence).

**Model label:** sonnet (mechanical CI-file edits + MIGRATION_NOTES authoring).

**Test plan:** version-consistency + tag-reference greps; CI green on the rebuilt image is the gate.

---

## Version discipline

**Sync sites (enforced by `scripts/check_versions.py` + `.pre-commit-config.yaml` always_run):**

| Field | Files | Current (master) | This train |
|---|---|---|---|
| Core version | `pyproject.toml`, `server.json` (×2), `flake.nix`, `docker-compose.yml` (`CORE_VERSION`), `uv.lock` | 5.130.0 | **5.131.0** |
| Backend version | `server.json` (`backend_version`), `docker-compose.yml` (`BACKEND_VERSION`) | 5.41.0 | **5.42.0** |

- **Backend bump justified two ways:** the CE salt bump touches `backend/embed_service/` →
  `check_backend_bump.py` fires automatically; and the CE stack behavior changes on the transformers
  bump — a backend behavior change. Both mandate the `backend_version` bump; the hook covers it.
- **Namespace verified clear** (2026-07-12): no local/remote branch claims 5.131.0 / 5.132.0 /
  backend 5.42.0 / `deps-modern*`. The T4 plan's own clearance check predated Car 0 (#188) landing —
  re-verified here.
- **Dockerfile.ci `ARG YADGAR_VERSION`** also moves to 5.131.0 (Car 2).

---

## Rollback plan

**Revert = restore the old lock.** Reverting the single train PR restores the pre-upgrade
`uv.lock` + `pyproject.toml` (transformers 4.57.6, hub 0.36.2, salt `"1"`) and the pre-upgrade
`Dockerfile.ci`/`ci-pr.yaml` tag. Because Ettin is NOT the prod default (this train changes no model),
a revert has **no prod-model-swap to undo** — it simply returns to the known-good pre-5.x stack. T4
(which would run after) is what a revert would *re-block*, by design.

**CI image rollback:** the old `yadgar-ci:<old-tag>` image still exists in the registry; reverting the
`ci-pr.yaml` tag reference points CI back at it. No image deletion needed. (This is why tag discipline
matters — the old tag is the rollback artifact.)

**Salt rollback:** reverting `CE_SCORING_VERSION "2"→"1"` re-buckets the CE cache to the old keys; the
discard-on-mismatch path handles the transition safely in both directions.

**Partial-fail fallback:** if the (e) drift arm shows embedding-space drift that would require a
store re-embed, the train STOPS at audit — it does not ship a silently-re-ranking stack. That is a
scope escalation to the user (re-embed decision), not a car of this train.

---

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| **hf-xet<1.4 cap blocks the resolve** (hub 1.x requires hf-xet≥1.5.1) | **Certain if unedited** — hard version collision (audit) | **Raise the cap to `<2.0` in the Car 1 pyproject edit — mandatory.** Resolved: Q3. Not a soft "verify" — the resolve fails or back-solves onto an ancient hub (which re-fires the gated deprecation) without it. |
| **huggingface_hub 1.x major bump floods new deprecations** (fatal under zero-warning gate) | **High** — forced 0.36→**≥1.3.0/≥1.5.0** major (PyPI-verified floor, higher than "≥1.0") | Car 1 triage pass; narrow message-pinned ignores in the ADR-0087 style; opus judges ignore-vs-fix. **The riskiest triage item** (the hf-xet cap above is the riskiest *resolve* item). |
| **Blanket re-lock re-triggers the 57-fail incident** (pulls starlette>1.0 / httpx testclient dep) | Medium if blanket; ~0 if targeted | **Targeted upgrade** (HF stack only; hold starlette/httpx/fastapi/uvicorn) — the primary mitigation. Documented at `Dockerfile.ci:64-70`. |
| **Silent CE score drift** (GTE tokenization changes under transformers 5.x, model id unchanged) | Medium-High | `CE_SCORING_VERSION "1"→"2"` salt bump busts the persistent cache; (e) drift arm confirms recall@k parity. |
| **Silent embedding-space drift** (MiniLM embeddings shift; stored DB embeddings on old stack) | Medium | (e) GTE-on-old vs GTE-on-new LongMemEval arm is the catch-all; drift → investigate re-embed BEFORE shipping (rollback plan escalation). |
| **py3.14 wheel gap** for transformers 5.x / tokenizers / torch | **Low** — verified: tokenizers abi3, torch cp314 CPU, transformers/st pure-python | Confirmed from PyPI; no action needed. CPU-only path avoids the CUDA-cp314 gap entirely. |
| **st incompatibility** with transformers 5.x | **Low** — st 5.4.1 already spans 4.x/5.x (`transformers>=4.41,<6`), dual CI | No st floor change needed; hold st unless triage forces a bump. |
| **`[onnx]` extra (optimum/onnxruntime) caps transformers or lacks py3.14 wheel** → targeted resolve fails on user's machine (past all gates) | **Low — pre-flighted clear** | optimum 2.1.0/2.2.0 pins `transformers>=4.29` no-ceiling (PyPI-verified); onnxruntime 1.27.0 cp314 wheels (draft live-check, trusted). Extra drags along in the same upgrade. **Audit Q8: KEEP the extra** — `ml_client.py:375 backend="onnx"` is a LIVE env-gated path; dropping the extra without removing that code = latent ImportError. Do not half-drop. |
| **flashrank breakage** on the bump | Low | flashrank uses a separate `ms-marco-MiniLM-L-12-v2` in `~/.cache/flashrank` (not the HF transformers path); load smoke (d) covers it. |
| **doc2query breakage** (`AutoModelForSeq2SeqLM`) | Low | API retained in 5.x; load smoke (d) covers it. |
| **Stale yadgar-ci tag** left at 5.121.1 → CI tests old deps | Medium if missed | Car 2 grep-verifies no `5.121.1` residue; CI-green-on-rebuilt-image gate catches divergence. |
| **CI-only divergence** (local green, CI red — the 57-fail class) | Medium | Frozen-lock bake means CI runs `uv.lock` exactly; the rebuilt-image CI-green gate is the real integration proof (local green is necessary, not sufficient). |

---

## Test plan (consolidated)

1. **Warning-triage suite** — full CI legs under `filterwarnings=["error"]` on the new lock; zero
   un-triaged warnings. (Reproduce the CI condition locally per the known gotcha: force backend
   unreachable — `env YADGAR_EMBED_URL=http://127.0.0.1:1 YADGAR_DB_URL=http://127.0.0.1:1 pytest …`
   — so no live-backend masks an unmocked call.)
2. **Model load smokes (non-mocked)** — Ettin-32m, Ettin-68m, GTE, MiniLM-embed, doc2query, flashrank
   each load+score/encode on the new stack. Run outside the CE-mocked CI legs (local or a dedicated
   non-mocked gate).
3. **CE salt test** — the existing Car 0 `monkeypatch(CE_SCORING_VERSION)` test stays green; add an
   assertion that the shipped salt is `"2"`. Salt + lock + pyproject edits land in ONE commit
   (zero stale-score window).
4. **Direct embed-drift probe (f)** — fixed-sentence cosine(old-stack, new-stack) ≥ 0.9999; old-stack
   vector captured on master BEFORE the lock flip. Cheap, deterministic, primary embed-drift catch.
5. **LongMemEval drift arm (e)** — GTE-on-old vs GTE-on-new, all 6 `--types`, small Q, determinism
   pre-check; **old-stack baseline captured on master before the branch** (recomputable-after-flip is
   impossible by design); recall@k parity table pasted into this plan.
6. **Version + tag consistency** — `check_versions.py` green; grep confirms no `5.121.1` residue
   across `.forgejo/` + `Dockerfile.ci-viz` (4 workflows + viz base); `transformers>=5.0` floor +
   `hf-xet<2.0` cap present.
7. **CI-green-on-rebuilt-image** — after the user rebuilds+pushes yadgar-ci (then yadgar-ci-viz),
   all legs green (the integration gate; the 57-fail incident was CI-only).

---

## Open questions — audit dispositions

Audit resolved every question it could (sizing/mechanics are audit calls; the user already chose the
train). Verdicts below; **only Q1 + Q5 remain genuine user calls.**

1. **Targeted vs blanket `uv lock --upgrade`** — **DECIDED: BLANKET (user call, 2026-07-12).**
   The user overrode the audit's targeted recommendation deliberately: full `uv lock --upgrade`,
   accepting the wider warning-triage surface (incl. possible starlette/TestClient churn of the
   57-fail class — triaged properly under the zero-warning gate, not avoided). Q6 still holds:
   if the blanket resolve floats sentence-transformers above 5.4.1, pin it back to 5.4.1. The
   audit's targeted recommendation stands above as the historical record of the tradeoff.
2. **`transformers>=5.0` explicit floor** — **AUDIT: RESOLVED — add it.** The Ettin requirement is
   load-bearing; a floor prevents a future re-lock silently dropping transformers back under st's
   wide `<6.0.0` bound. Mechanical, correct, in Car 1. No user call needed.
3. **hf-xet<1.4 under hub 1.x** — **AUDIT: RESOLVED — the cap MUST rise to `<2.0` (mandatory, not
   optional).** hub 1.x requires `hf-xet>=1.5.1` (PyPI hub 1.23.0), so `<1.4` collides with the
   modern-hub resolve. The pin's original reason (old-hub `download_files()`) dissolves at hub ≥1.5.
   Raise the cap in the Car 1 pyproject edit. No user call — it's a hard resolution requirement.
4. **`yadgar-ci-viz` coordinated rebuild** — **AUDIT: RESOLVED — YES.** `Dockerfile.ci-viz` does
   `FROM yadgar-ci:5.121.1` (hardcoded base). Rebuild AFTER yadgar-ci, bump `FROM`+LABEL, both build
   commands in MIGRATION_NOTES. Handled in Car 2.
5. **Embed-drift contingency** — **AUDIT: partly resolved.** Mechanically, this plan treats a
   store re-embed as an escalation/STOP (not a car) — correct sizing. The audit ADDED a direct
   embed-drift probe (gate (f)) so drift is caught cheaply and deterministically before the LongMemEval
   arm. *User call retained: IF the probe/arm shows real embedding-space drift, whether to authorize a
   store re-embed (a data migration) is a user decision — the train STOPS and asks, does not ship.*
6. **st bump to 5.6.0?** — **AUDIT: RESOLVED — HOLD.** st 5.4.1 already spans transformers 4.x/5.x
   (`>=4.41.0,<6.0.0`) and carries the CrossEncoder modular arch Ettin needs. Let the resolver hold it
   unless triage forces a bump. No user call.
7. **T4 renumber** — **AUDIT: RESOLVED (hand-off confirmed).** This train takes 5.131.0 / backend
   5.42.0 (namespace verified clear 2026-07-12). T4's body (currently mid-rewrite in the working tree)
   re-claims the next slot in its OWN audit. This plan does NOT edit T4's version tables (T4 is `M` in
   the working tree — untouched). The two plans are cross-consistent (T4 already documents the same
   stale-tag + Car-0-landed facts). No user call.
8. **Drop the `[onnx]` extra?** — **AUDIT: RESOLVED — KEEP the extra (do NOT half-drop).** ADR-0043
   rejected onnx-int8 (2× slower) and ADR-0067 "removed the onnx backend" — BUT `ml_client.py:375`
   STILL has a live `backend="onnx"` code path, env-gated behind `GTE_RERANKER_BACKEND` (default
   "torch"). Dropping `[onnx]` while that code + the `GTE_RERANKER_BACKEND=onnx-int8` config option
   remain = a **latent ImportError** the instant anyone flips the env var. This is binary: **keep the
   extra (recommended — harmless, and it's the plan's own onnx fallback)**, OR drop the extra AND
   remove the onnx code path + config option together (out of scope for this train). Half-drop is a
   regression. **Recommend KEEP** — which keeps onnxruntime cp314 wheels load-bearing (trusted from
   the draft's live PyPI check; load-smoke (d) would catch a gap). No user call unless the user wants
   the full onnx removal as a separate cleanup.

   **BUILD-TIME CORRECTION (2026-07-12, resolver-observed): KEEP is IMPOSSIBLE — full removal
   executed.** The audit verified `optimum` (`transformers>=4.29`, no ceiling) but the resolver
   routes `sentence-transformers[onnx]` through the split package **`optimum-onnx`**, whose latest
   release (0.1.0) pins `transformers>=4.36,<4.58.0` (PyPI-verified; every st version's [onnx]
   extra hits the same wall). `[onnx]` + `transformers>=5.0` is therefore UNSATISFIABLE — the
   pre-flight `uv lock --upgrade` fails outright. Audit claims #8/#19 checked the wrong package.
   Per this question's own binary (half-drop forbidden), the build drops the extra AND removes the
   dormant onnx path together: `GTE_RERANKER_BACKEND`/`GTE_RERANKER_ONNX_FILE` knobs (Settings +
   registry + FIELD_META + docs row), the `backend="onnx"` branch + `OnnxRerankerUnavailableError`
   in `ml_client.py`, and the two onnx-int8 tests. optimum/onnxruntime/onnx leave the lock.
   Re-adding ONNX reranking requires an optimum-onnx release supporting transformers 5.x.
9. **`load_in_4bit` benchmark fix** — **AUDIT: RESOLVED — defer, non-blocking.** `run_locomo_jscore.py`
   is a test-only benchmark, not a shipped path or (confirmed) a CI leg. The `apply_chat_template`
   return-type change (`BatchEncoding` vs bare ids) is a one-line fix IF that benchmark ever runs;
   fix opportunistically, does not gate the train.

---

## Sources

- **Blocker premise (primary):** Ettin model cards + raw `config.json`/`tokenizer_config.json` for
  `cross-encoder/ettin-reranker-32m-v1` and `-68m-v1` (`tokenizer_class: "TokenizersBackend"`,
  `transformers_version: "5.7.0"`, max context 7999); **empirically reproduced** load failure on the
  current 4.57.6 pin (two ValueErrors: TokenizersBackend + AutoProcessor).
- **transformers 5.x:** `MIGRATION_GUIDE_V5.md` + release notes (github.com/huggingface/transformers);
  PyPI (5.13.1 latest, July 2026); min Python ≥3.10, torch ≥2.4; `TRANSFORMERS_CACHE`→`HF_HOME`;
  `huggingface-hub>=1.3.0,<2.0` (5.0.0) / `>=1.5.0,<2.0` (5.13.1) — PyPI-verified in audit;
  `tokenizers>=0.22.0,<=0.23.0` (5.13.1); httpx HTTP backend; pipeline removals / Flax-TF sunset (none used by yadgar).
- **hf-xet × hub (audit, primary):** PyPI hub 1.23.0 `requires_dist` → `hf-xet>=1.5.1,<2.0` on
  x86_64/arm64. Forces the `hf-xet<1.4`→`<2.0` cap raise. optimum 2.2.0 → `transformers>=4.29` (no ceiling).
- **sentence-transformers:** pyproject `transformers>=4.41.0,<6.0.0`; dual 4.x/5.x CI since 5.2.1;
  st 5.4.0 CrossEncoder modular arch (5.4.1 sufficient for Ettin); latest 5.6.0.
- **py3.14 wheels:** tokenizers 0.23.1 cp310-abi3 (covers 3.14); torch 2.13.0 cp314 CPU; PyPI.
- **httpx/starlette:** httpx 0.28.1 latest stable, 1.0 dev-only, repo issues closed Feb 2026;
  starlette 1.0rc1 (Feb 2026), no 1.1+; `AsyncClient(app=)` removed at httpx 0.28.0 (already migrated).
- **Repo facts (grep/read against master 5.130.0):** pins (`pyproject.toml`/`uv.lock`); transformers
  usage sites (`_seq2seq.py:22-26`, `embeddings.py:131-152`, `ml_client.py:359-376`,
  `test_phase2_subsystems.py:832-836`, `run_locomo_jscore.py:255-275`); httpx modern-pattern sites;
  zero-warning gate (`pyproject.toml:221-240`, ADR-0087); lock-parity bake + 57-fail comment
  (`Dockerfile.ci:64-76`); stale CI tag (`ci-pr.yaml:44/94/…`); CE salt + `_ckpt` fix on master
  (`embed_service.py:206,227`, Car 0 #188); env surface (`HF_HOME`/`HF_HUB_OFFLINE` used,
  `TRANSFORMERS_CACHE` unused).
- **Style/structure exemplar:** `docs/plans/t4-ettin-train-2026-07-12.md`.
- **ADRs:** ADR-0087 (zero-warning gate), ADR-0088 (the "train = one PR / one version" convention
  this train follows — cited as convention, not as this train's ADR), ADR-0043 (onnx-int8 reranking
  REJECTED — 2× slower) + ADR-0067 ("onnx backend removed") — BUT audit found `ml_client.py:375
  backend="onnx"` is still LIVE + env-gated, so the `[onnx]` extra is dormant not dead (Q8: keep it). A NEW deps-modernization ADR (next free number — T4 references
  up to ADR-0098, so **ADR-0099+**) should be written at build time capturing the targeted-upgrade
  decision + the transformers-5.x-forces-hub-1.0 blast-radius + the `CE_SCORING_VERSION` salt-bump
  rationale.
