# License Compliance Audit — 2026-05-30

**Purpose:** Verify that yadgar's intended adoption of competitor patterns, datasets, and runtime libraries
(per `docs/competitor-audit-2026-05-30.md` and the 6 Adopt items) is license-compliant. Audit follows
the protocol in `docs/AUDIT_DECISIONS.md`.

**Yadgar version at time of audit:** v5.10.3 (deployed 2026-05-29).

**Auditor:** automated research + analysis, 2026-05-30.

---

## Yadgar's Own License

Yadgar is dual-identified in its codebase:

- **`LICENSE` file** (root): Apache License 2.0, Copyright 2026 maxagahi.
- **`pyproject.toml`** `license` field: `"MIT"` — an inconsistency.

The `LICENSE` file is the legally operative document; the `pyproject.toml` classifier
(`License :: OSI Approved :: MIT License`) appears to be a copy-paste error during project setup.
The repo's actual code is governed by Apache 2.0.

**Required action:** Correct `pyproject.toml` `license` field to `"Apache-2.0"` and update
the classifier to `License :: OSI Approved :: Apache Software License`. This is a housekeeping fix;
it does not change what yadgar ships, but it prevents confusion for any downstream consumer
who reads package metadata.

**Apache 2.0 header in LICENSE:**
```
Apache License
Version 2.0, January 2004
http://www.apache.org/licenses/

Copyright 2026 maxagahi
```

Source: `/home/max/git/yadgar/LICENSE` (local; homepage `https://codeberg.org/maxagahi/yadgar`).

---

## Compatibility Matrix

| Item | License | Yadgar use type | Verdict | Required action |
|---|---|---|---|---|
| mem0 | Apache 2.0 | Pattern inspiration only | GREEN | None |
| Chroma | Apache 2.0 | Comparison only, no adoption | GREEN | None |
| Pinecone | Proprietary SaaS | No code dependency, no API call | GREEN | None |
| Zep / Graphiti | Apache 2.0 | Pattern inspiration only | GREEN | None |
| Letta (MemGPT) | Apache 2.0 | Pattern inspiration only | GREEN | None |
| LongMemEval dataset | MIT | Benchmark eval + published numbers | GREEN | Attribution in benchmark README |
| LoCoMo dataset | CC BY-NC 4.0 | Benchmark eval + published numbers | YELLOW | Non-commercial use only; attribution required; see §Datasets |
| DuckDB | MIT | Runtime dep (Adopt-6) | GREEN | Copyright notice in NOTICE file or README |
| SurrealDB (runtime) | BSL 1.1 + Python SDK Apache 2.0 | Embedded storage, not offered as DBaaS | GREEN (conditional) | Document the "not-DBaaS" boundary; see §SurrealDB |
| pgvector | PostgreSQL License (BSD-like) | Alternative storage (v5.20.0 possible) | GREEN | None |
| highlight.js | BSD 3-Clause | Vendored JS lib (v5.12.0) | YELLOW | Include copyright notice in vendored file |
| marked | MIT | Vendored JS lib (v5.12.0) | GREEN | Include copyright notice in vendored file |
| DOMPurify | Apache 2.0 OR MPL 2.0 (dual) | Vendored JS lib (v5.12.0, optional) | GREEN | None |
| OpenTelemetry SDKs | Apache 2.0 | Runtime dep (v5.6.3+) | GREEN | None |
| sentence-transformers | Apache 2.0 | Runtime dep (ML extras) | GREEN | None |
| cross-encoder/nli-deberta-v3-small | Apache 2.0 | ML model (NLI reranker stage) | GREEN | None |
| networkx, numpy, scipy | BSD 3-Clause | Runtime deps | GREEN | None |

---

## Per-Item Analysis

### 1. mem0 (Adopt-2)

**License:** Apache 2.0
**Source:** `https://github.com/mem0ai/mem0/blob/main/LICENSE`
**Copyright:** Taranjeet Singh, 2023

**What yadgar consumed:** The write-time LLM conflict resolution pattern (v5.3.4 C4 `YADGAR_CONFLICT_RESOLVER`).
No code was copied. The pattern — "run LLM on write to detect contradictions with existing memories,
invalidate the loser" — is an architectural idea, not copyrightable expression. Yadgar's implementation
is independent Python code gated behind a feature flag.

**Compliance verdict: GREEN**

Apache 2.0 to Apache 2.0 (yadgar's license). No code copy means no distribution obligation applies.
Even if code had been copied, Apache 2.0 ↔ Apache 2.0 is fully compatible.

**Required action:** None.

---

### 2. Chroma

**License:** Apache 2.0
**Source:** `https://github.com/chroma-core/chroma/blob/main/LICENSE`

**What yadgar consumes:** Comparison only. The competitor audit benchmarks yadgar against Chroma
conceptually (e.g., recall precision comparisons). No code dependency, no data import, no API call.

**Compliance verdict: GREEN**

Benchmarking a competitor by running your own system against a shared dataset is not a license issue.
Quoting public Chroma benchmark numbers in yadgar docs requires no license action.

**Required action:** None.

---

### 3. Pinecone

**License:** Proprietary SaaS (closed-source)
**Source:** `https://www.pinecone.io/legal/master-subscription-agreement/`

**What yadgar consumes:** Nothing. Pinecone is a managed vector database; yadgar does not call
Pinecone's API, embed the Pinecone SDK, or use Pinecone data. The competitor audit mentions Pinecone
purely for landscape comparison.

**Note on Pinecone's ToS for benchmarking:** Pinecone's Assistant ToS prohibits using Pinecone
to develop competing datasets or models. This restriction does not apply to yadgar since yadgar
does not use Pinecone at all.

**Compliance verdict: GREEN**

**Required action:** None.

---

### 4. Zep / Graphiti (Adopt-3)

**License:** Apache 2.0
**Source:** `https://github.com/getzep/graphiti/blob/main/LICENSE`

**What yadgar consumed:** Bi-temporal fact windows pattern (v5.3.4 C1) and citation tracing
(v5.3.4 C3). Specifically: `valid_from` / `valid_until` on KG edges, and `source_memory_id`
foreign key on edges. These are schema patterns — not code copies — implementing a general
database design pattern (bi-temporal modeling, which predates Zep).

**Compliance verdict: GREEN**

Bi-temporal modeling is a decades-old database pattern (Snodgrass 1995). Zep's implementation
of it is Apache 2.0 anyway. No code was copied; yadgar wrote its own migrations.

**Required action:** None.

---

### 5. Letta / MemGPT (Adopt-4)

**License:** Apache 2.0
**Source:** `https://github.com/letta-ai/letta/blob/main/LICENSE`
**Copyright:** Letta authors, 2023

**What yadgar plans to consume (Adopt-4):** Memory blocks pattern — agent-readable named memory
slots (e.g., `persona`, `human`, `core_memory`). As of v5.10.3, Adopt-4 is still a plan
(`docs/PLAN_V5_4_to_v7.md` deferred to v6 under "Letta-style agent-self-edit-at-inference").
No implementation shipped yet.

**Compliance verdict: GREEN** (for pattern adoption)

The memory blocks concept is an architectural pattern. Even if yadgar copies specific
implementation ideas (named slots, in-context vs. archived memory tiers), this is not
code copying. Apache 2.0 to Apache 2.0 would be compatible if code were ever copied.

**Required action:** None. If actual Letta source code is ever copied verbatim, add Apache 2.0
attribution in the relevant file headers and a NOTICE entry.

---

### 6. LongMemEval Dataset (Adopt-1)

**License:** MIT
**Source:** `https://huggingface.co/datasets/xiaowu0162/longmemeval` and
`https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned`
**GitHub:** `https://github.com/xiaowu0162/LongMemEval/blob/main/LICENSE`

**What yadgar consumes:** Benchmark evaluation suite. `benchmarks/run_longmemeval.py` runs yadgar
against LongMemEval questions. Adopt-1 (`docs/PLAN_V5_13_0_BENCHMARK_PUBLICATION.md`) plans to
publish yadgar's LongMemEval scores publicly.

**Note:** The original `xiaowu0162/longmemeval` dataset is deprecated in favor of
`xiaowu0162/longmemeval-cleaned`, which removes noisy history sessions. Both are MIT licensed.

**Compliance verdict: GREEN**

MIT license allows commercial and non-commercial use, modification, and redistribution.
Running a benchmark against MIT data and publishing results is fully permitted.
Attribution required: include the dataset citation in published benchmark reports.

**Required action:** Add the following citation to `benchmarks/README.md` and any
published benchmark report (Adopt-1 publication):

```
LongMemEval: Wu et al., "LongMemEval: Benchmarking Chat Assistants on Long-Term
Interactive Memory", ICLR 2025. Dataset: https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned
(MIT License).
```

---

### 7. LoCoMo Dataset (Adopt-1)

**License:** Creative Commons Attribution-NonCommercial 4.0 International (CC BY-NC 4.0)
**Source:** `https://raw.githubusercontent.com/snap-research/locomo/main/LICENSE.txt`
**Confirmed via:** LICENSE.txt in snap-research/locomo repository.

**What yadgar consumes:**
- `benchmarks/run_locomo_jscore.py` — primary regression suite
- `benchmarks/run_locomo_ablation.py` — ablation study
- `benchmarks/run_benchmark_gpu.py` — GPU-accelerated reranker path
- `benchmarks/test_e_locomo.py` — end-to-end smoke test

Adopt-1 (`docs/PLAN_V5_13_0_BENCHMARK_PUBLICATION.md`) plans to publish yadgar's LoCoMo scores.

**Compliance verdict: YELLOW**

CC BY-NC 4.0 permits:
- Running evaluations internally — YES, permitted
- Publishing benchmark numbers in academic/non-commercial contexts — YES, permitted
- Commercial publication (e.g., paid product marketing, investor decks, commercial white papers) — RESTRICTED

The restriction is: "NonCommercial purposes only." If yadgar is a commercial product or if
the benchmark publication is used to drive commercial sales, this is a gray area that requires
legal review. For a personal/open-source project published on Codeberg, academic-style
publication of benchmark numbers is clearly fine. The moment yadgar's benchmark numbers appear
in a commercial pitch deck or paid product page, the NC restriction applies.

**Required action:**
1. Add citation to `benchmarks/README.md`:
   ```
   LoCoMo: Maharana et al., "Evaluating Very Long-Term Conversational Memory of LLM Agents",
   ACL 2024. Dataset: https://github.com/snap-research/locomo (CC BY-NC 4.0).
   ```
2. In Adopt-1 benchmark publication doc, add a section "Dataset License Notice" that states:
   "LoCoMo benchmark results reported under CC BY-NC 4.0 terms (non-commercial use).
   Yadgar's benchmark evaluation code does not redistribute the LoCoMo dataset."
3. If yadgar ever transitions to a commercial product, obtain explicit permission from
   SNAP Research or switch to a permissively-licensed dataset for benchmark publishing.
4. **Do NOT redistribute LoCoMo data** in yadgar releases (no bundling dataset files in the repo).
   The `benchmarks/README.md` already correctly points users to download from HuggingFace,
   not from yadgar's repo.

---

### 8. DuckDB (Adopt-6)

**License:** MIT License
**Source:** `https://raw.githubusercontent.com/duckdb/duckdb/main/LICENSE`
**Copyright:** Stichting DuckDB Foundation, 2018-2026

**What yadgar would consume:** Adopt-6 plans a DuckDB-based memory export feature. DuckDB
would be a runtime dependency (likely via the `duckdb` Python package, also MIT).

**Compliance verdict: GREEN**

MIT is maximally permissive. No restrictions on commercial or non-commercial use. The only
requirement is preserving copyright notices in distributed copies.

**Required action:** When adding `duckdb` as a dependency in `pyproject.toml`, add a NOTICE
entry or README acknowledgment:
```
DuckDB — MIT License — Copyright Stichting DuckDB Foundation 2018-2026
https://github.com/duckdb/duckdb
```
No NOTICE file is currently required by Apache 2.0 (yadgar's license), but one is recommended
best practice for any external dependencies yadgar redistributes.

---

## Runtime Libs Already In Yadgar

### SurrealDB (BSL 1.1 — Most Complex)

**SurrealDB server binary:** Business Source License 1.1
**SurrealDB Python SDK (`surrealdb` PyPI package):** Apache 2.0
**Source:** `https://surrealdb.com/license` and `https://github.com/surrealdb/surrealdb.py/blob/main/LICENSE`

**Yadgar's current use pattern:**
- `pyproject.toml`: `surrealdb>=1.0.0` (promoted from dev-only in v5.10.2)
- Usage: yadgar connects to a SurrealDB instance as its storage backend. Yadgar itself is not
  in the business of offering SurrealDB-as-a-service to third parties.

**BSL 1.1 — the restriction:**
SurrealDB's BSL prohibits providing SurrealDB as a "managed database service" (DBaaS) to
third parties without a commercial license. The Additional Use Grant explicitly permits:
- Embedding SurrealDB in your own application
- Using SurrealDB internally (employees/contractors)
- Distributing applications to customers where SurrealDB is a component
- Scaling to any number of nodes

The restriction is ONLY: "You may not provide SurrealDB as a managed Database Service
(DBaaS) without a commercial agreement with SurrealDB Ltd."

**Change Date:** SurrealDB 3.0 converts to Apache 2.0 on January 1, 2030
(four years from first public release of that version).

**Yadgar's compliance verdict: GREEN (conditional)**

Yadgar is a personal/developer memory engine. Users install yadgar and it connects to their
own SurrealDB instance. Yadgar does NOT:
- Offer SurrealDB as a managed service to third-party customers
- Resell SurrealDB access
- Provide a "create your database" SaaS product

This use pattern is explicitly permitted by SurrealDB's BSL Additional Use Grant.

**Important: the Python SDK (`surrealdb` package) is separately licensed Apache 2.0.**
This is the actual pip dependency yadgar ships. The BSL applies to the SurrealDB server binary,
which yadgar users run themselves. Two distinct licensing layers.

**Required action:**
1. Document in `README.md` or `docs/configuration.md` that yadgar requires a user-provided
   SurrealDB instance, licensed under BSL 1.1, and link to `https://surrealdb.com/license`.
2. Maintain the current usage pattern (user-operated SurrealDB, never "yadgar as DBaaS").
3. **Revisit trigger:** if yadgar ever offers a hosted/managed service, this becomes RED
   without a SurrealDB commercial license.
4. The Python SDK dep is clean (Apache 2.0) — no action needed for the PyPI package itself.

---

### pgvector

**License:** PostgreSQL License (BSD-like, 2-clause)
**Source:** `https://raw.githubusercontent.com/pgvector/pgvector/master/LICENSE`
**Copyright:** PostgreSQL Global Development Group

**What yadgar considers:** `docs/PLAN_V5_20_0_ROADMAP_FRESHNESS.md` and the competitor audit
discuss pgvector as an alternative storage backend (possible v5.20.0 migration).

**Compliance verdict: GREEN**

PostgreSQL License is a permissive BSD-style license. Compatible with Apache 2.0.
No special conditions for use as a runtime dependency.

**Required action:** None. If pgvector is adopted as a dep, no additional compliance steps needed.

---

### highlight.js (v5.12.0 Wiki Bookmarks — vendored)

**License:** BSD 3-Clause
**Source:** `https://raw.githubusercontent.com/highlightjs/highlight.js/main/LICENSE`
**Copyright:** Ivan Sagalaev, 2006

**What yadgar plans:** Vendored in `yadgar/static/lib/highlight.min.js` for the Wiki Bookmarks
page (v5.12.0).

**Compliance verdict: YELLOW**

BSD 3-Clause requires:
1. Retain the copyright notice in all copies
2. Retain the copyright notice in documentation
3. Do not use copyright holder's name for endorsement

When vendoring minified JS, the copyright comment is typically stripped by the minifier.
The `.min.js` file must either include the license header comment or be accompanied by
`highlight.min.js.LICENSE.txt` in the same directory.

**Required action:**
- Use the CDN-hosted version with SRI hash (already planned per v5.3.9 item 8 — SRI hashes
  on CDN scripts) OR include a `highlight.min.js.LICENSE.txt` alongside the vendored file.
- CDN approach is simpler and shifts distribution responsibility to the CDN.
- If vendoring: ensure `highlight.min.js` retains the `/*! highlight.js v<version> (BSD-3)
  Copyright (c) Ivan Sagalaev */` header comment (hljs builds typically include it by default).

---

### marked (v5.12.0 Wiki Bookmarks — vendored)

**License:** MIT License
**Source:** `https://marked.js.org/license` and `https://github.com/markedjs/marked/blob/master/LICENSE.md`
**Copyright:** MarkedJS (2018+), Christopher Jeffrey (2011-2018)

**What yadgar plans:** Vendored in `yadgar/static/lib/marked.min.js` (v5.12.0).

**Compliance verdict: GREEN**

MIT permits use, modification, and distribution. The only requirement is preserving the
copyright notice in distributed copies. MIT license header should be retained in the
vendored file, but this is a soft requirement rarely enforced.

**Required action:** Retain or include a short license notice. Best practice: include a
`marked.min.js.LICENSE.txt` file alongside the vendored file, or use CDN with SRI hash.

---

### DOMPurify (v5.12.0 Wiki Bookmarks — optional vendoring)

**License:** Apache 2.0 OR MPL 2.0 (dual license — consumer's choice)
**Source:** `https://github.com/cure53/DOMPurify/blob/main/LICENSE` (Apache 2.0) and
`https://github.com/cure53/DOMPurify/blob/main/LICENSE-MPL` (MPL 2.0)
**Confirmed:** Both LICENSE and LICENSE-MPL files exist in the cure53/DOMPurify root.
Consumer may choose either license.

**What yadgar plans:** Optional vendored in `yadgar/static/lib/dompurify.min.js` (v5.12.0).

**Compliance verdict: GREEN**

Dual Apache 2.0 / MPL 2.0 — yadgar can choose Apache 2.0, making it Apache-to-Apache
compatible (yadgar's own license is Apache 2.0). Alternatively, MPL 2.0 is also compatible
with Apache 2.0 for embedding purposes (MPL 2.0 §3.3 explicitly allows combining with
Apache 2.0-licensed code).

**Required action:** None. When vendoring, include a brief comment indicating which license
was selected (Apache 2.0 recommended for consistency with yadgar's own license).

---

### OpenTelemetry SDKs

**Packages in pyproject.toml:**
- `opentelemetry-api>=1.30,<2`
- `opentelemetry-sdk>=1.30,<2`
- `opentelemetry-instrumentation-fastapi>=0.51b0`
- `opentelemetry-instrumentation-httpx>=0.51b0`
- `opentelemetry-exporter-otlp-proto-http>=1.30,<2`

**License:** Apache 2.0 (all OpenTelemetry packages, all implementations)
**Source:** `https://github.com/open-telemetry/opentelemetry-python/blob/main/LICENSE`

**Compliance verdict: GREEN**

All OTel packages are Apache 2.0 — fully compatible with yadgar's Apache 2.0 license.

**Required action:** None.

---

### sentence-transformers + torch

**sentence-transformers:**
- License: Apache 2.0
- Source: `https://github.com/UKPLab/sentence-transformers/blob/master/LICENSE`

**torch (PyTorch):**
- License: BSD-style (PyTorch License, a 3-clause BSD variant)
- Torch is an optional transitive dep via sentence-transformers

**Compliance verdict: GREEN** for both.

**Required action:** None. sentence-transformers is an optional dep (`yadgar[ml]`) — users
who do not install ML extras do not encounter it.

---

### cross-encoder/nli-deberta-v3-small (model weights)

**License:** Apache 2.0
**Source:** `https://huggingface.co/cross-encoder/nli-deberta-v3-small`

**What yadgar consumes:** Model weights downloaded at runtime via HuggingFace Hub for the NLI
reranker stage. Weights are loaded by `yadgar/retrieval/reranking.py` (likely via sentence-transformers).

**Note on the SNLI/MultiNLI training data:** The model was trained on SNLI and MultiNLI datasets.
SNLI is CC BY-SA 4.0 (permits commercial use with attribution). MultiNLI is similar. These are
training data licenses for the model's *creators* — they do not restrict downstream *users* of
the model. The model's own license is Apache 2.0.

**Compliance verdict: GREEN**

Apache 2.0 weights, runtime download (not bundled in yadgar repo), commercial use permitted.

**Required action:** None. Model is never bundled into yadgar distribution.

---

### networkx, numpy, scipy

| Package | License | |
|---|---|---|
| networkx | BSD 3-Clause | Compatible with Apache 2.0 |
| numpy | BSD 3-Clause | Compatible with Apache 2.0 |
| scipy | BSD 3-Clause | Compatible with Apache 2.0 |

**Compliance verdict: GREEN** for all three.

**Required action:** None.

---

## Datasets Specifically

This section consolidates dataset analysis with extra attention given the Adopt-1
benchmark publication plan (`docs/PLAN_V5_13_0_BENCHMARK_PUBLICATION.md`).

### LongMemEval — MIT — GREEN

- Publishing benchmark scores: **fully permitted**.
- Attribution required in any publication: cite Wu et al. (ICLR 2025).
- No restrictions on commercial vs. non-commercial context.
- Use `longmemeval-cleaned` (not deprecated `longmemeval`) for current benchmarking.

### LoCoMo — CC BY-NC 4.0 — YELLOW (critical for Adopt-1)

- Running evals internally: **permitted**.
- Publishing scores in open-source / academic context: **permitted with attribution**.
- Publishing scores in commercial product materials: **RESTRICTED without permission**.
- Distributing the dataset or bundling it in yadgar: **prohibited without permission**.

**Adopt-1 risk:** The plan notes publishing benchmark numbers for community trust. If yadgar
is purely a personal open-source project, CC BY-NC 4.0 publication is fine. If any commercial
activity is attached (consulting, paid hosting, commercial licensing), obtain written permission
from SNAP Research (Stanford) BEFORE publishing LoCoMo-based numbers in those contexts.

**Yadgar already handles this correctly:** `benchmarks/README.md` says "gated by license; see
https://huggingface.co/datasets/snap-stanford/locomo etc." — the dataset is not bundled.
Maintain this separation.

### Other datasets referenced in benchmarks/

The following are NOT found in `benchmarks/` scripts at this time, but the audit task mentions them
for completeness:

- **BEAM:** Not found in yadgar benchmarks directory. No action needed.
- **HotpotQA:** Not found in yadgar benchmarks directory. Apache 2.0 if used.
- **MS MARCO:** Not found in yadgar benchmarks directory. Microsoft Research License if used
  (non-commercial research only) — requires review before use in benchmark publication.

If future benchmark work adds these datasets, re-audit at that time.

---

## SurrealDB BSL Implications (Detailed)

SurrealDB's BSL is the most complex license in yadgar's stack. Key facts established:

**Two licenses, two components:**

| Component | License | Governed by |
|---|---|---|
| SurrealDB server binary (the DB process) | BSL 1.1 | SurrealDB Ltd |
| `surrealdb` Python package (PyPI) | Apache 2.0 | SurrealDB Ltd |

**The BSL "Additional Use Grant" (verbatim from surrealdb.com/license):**
> "You may use the SurrealDB software to scale to any number of nodes, use or embed SurrealDB
> in your applications (whether you ship those applications to customers or run them as a
> service), and even run it as an internal service for use by your employees and contractors."

**The single restriction:**
> "You may not provide SurrealDB as a managed service, or a database-as-a-service (DBaaS),
> without an agreement with SurrealDB Ltd."

**Yadgar's use pattern analysis:**

- Yadgar connects to a SurrealDB instance that users run themselves. → **Permitted.**
- Yadgar distributes Docker images that include SurrealDB. → Permitted (embedding in application).
- Yadgar's `pyproject.toml` lists `surrealdb>=1.0.0` as a dep. → The Python SDK is Apache 2.0,
  not the BSL. The SDK connects to the server; it is not the server binary. → **Permitted.**
- If yadgar ever launched `yadgar.cloud` where users get a hosted yadgar instance with a managed
  SurrealDB backend they don't control → **WOULD BE PROHIBITED** under BSL without commercial license.

**Conversion timeline:** SurrealDB 3.0 (the version that introduced BSL) converts to Apache 2.0
on January 1, 2030. After that date, no restrictions apply.

**Conclusion:** Yadgar's current and planned use (v5.x through v6.x) is fully BSL-compliant.
The only trigger for non-compliance is adding a hosted/managed service offering.

---

## Cross-Reference with Adopt Plans

| Adopt item | Plan doc | License verdict | Notes |
|---|---|---|---|
| **Adopt-1** — LongMemEval + LoCoMo benchmark publication | `PLAN_V5_13_0_BENCHMARK_PUBLICATION.md` | LongMemEval GREEN, LoCoMo YELLOW | Add citations; non-commercial context only for LoCoMo |
| **Adopt-2** — mem0 write-time conflict pattern | Already shipped (v5.3.4) | GREEN | Pattern only, no code copy, Apache 2.0 compatible |
| **Adopt-3** — Zep/Graphiti bi-temporal pattern | Already shipped (v5.3.4) | GREEN | Pattern only, no code copy, Apache 2.0 compatible |
| **Adopt-4** — Letta memory blocks pattern | Deferred to v6 | GREEN | Pattern adoption; Apache 2.0 compatible if code ever copied |
| **Adopt-5** — JS SDK | Not found in current docs; v5.12.0 vendors highlight.js, marked, DOMPurify | YELLOW (highlight.js) | BSD-3 copyright notice required in vendored file |
| **Adopt-6** — DuckDB export | Not yet shipped | GREEN | MIT license, minimal obligation |

---

## Yadgar License Inconsistency (High Priority Fix)

As noted in "Yadgar's Own License" section: `pyproject.toml` declares `license = "MIT"` but
`LICENSE` file is Apache 2.0. This inconsistency:

1. Will mislead automated license scanners (e.g., pip-licenses, FOSSA, Snyk) into thinking
   yadgar is MIT when it is Apache 2.0.
2. Could confuse downstream consumers who install via PyPI (if yadgar is ever published there).
3. Is internally contradicted by the `classifiers` field which says
   `License :: OSI Approved :: MIT License`.

**Fix:** In `pyproject.toml`, change `license = "MIT"` to `license = "Apache-2.0"` and update
the classifier to `License :: OSI Approved :: Apache Software License`.

---

## Required Follow-Up Actions (Ranked by Urgency)

### Priority 1 — Fix now (data integrity / legal exposure)

1. **Fix `pyproject.toml` license field:** Change `"MIT"` → `"Apache-2.0"` and classifier.
   Prevents confusion about yadgar's actual license. One-line change, trivial PR.

2. **LoCoMo NC restriction documented in Adopt-1 plan:** Before publishing LoCoMo benchmark
   numbers in any commercial context, add a "Dataset License Notice" section to
   `PLAN_V5_13_0_BENCHMARK_PUBLICATION.md` and any published report. Non-commercial publication
   is fine as-is; the risk is if yadgar ever commercializes.

### Priority 2 — Before shipping (v5.12.0 Wiki Bookmarks)

3. **highlight.js copyright notice when vendoring:** Use CDN with SRI hash (already planned) OR
   include `highlight.min.js.LICENSE.txt`. BSD-3 requires copyright notice retention.
   Without it, yadgar is technically in violation of highlight.js's license.

4. **Add dataset citations to `benchmarks/README.md`:** Required by MIT (LongMemEval) and
   CC BY-NC 4.0 (LoCoMo) attribution clauses. One-paragraph addition.

### Priority 3 — Documentation / best practice

5. **SurrealDB BSL disclosure in `README.md` / `docs/configuration.md`:** Note that yadgar
   requires a user-provided SurrealDB instance (BSL 1.1) and link to the license page.
   Transparent for users who need to assess their own BSL compliance.

6. **DuckDB MIT notice when Adopt-6 ships:** Add brief attribution in NOTICE or README when
   DuckDB dep is added.

7. **marked copyright notice when v5.12.0 ships:** Include `marked.min.js.LICENSE.txt` alongside
   vendored file, or use CDN with SRI hash.

### Priority 4 — If yadgar ever commercializes

8. **LoCoMo CC BY-NC 4.0:** Obtain written permission from SNAP Research before including
   LoCoMo-based benchmark numbers in commercial materials.

9. **SurrealDB BSL:** Obtain commercial license from SurrealDB Ltd before offering any managed
   hosted service where SurrealDB is part of the backend.

---

## Final Recommendations Summary

**GREEN items (no action needed today):** mem0, Chroma, Pinecone, Zep/Graphiti, Letta,
LongMemEval, DuckDB, SurrealDB (current use), pgvector, marked, DOMPurify, OpenTelemetry,
sentence-transformers, nli-deberta-v3-small, networkx/numpy/scipy.

**YELLOW items (conditions apply):**
- **LoCoMo** (CC BY-NC 4.0): non-commercial benchmark publication only; attribution required.
- **highlight.js** (BSD-3): copyright notice must survive vendoring or use CDN.
- **yadgar itself** (Apache 2.0 LICENSE vs MIT in pyproject.toml): fix the inconsistency.

**RED items:** None identified.

**Most concerning finding:** LoCoMo dataset's CC BY-NC 4.0 license is the highest-risk item
if yadgar ever moves commercial. The fix is cheap (add attribution, note the restriction),
but the exposure is real if yadgar's benchmark publication is used in commercial contexts
without explicit SNAP Research permission.

**Second most concerning:** yadgar's own license inconsistency (`pyproject.toml` says MIT,
`LICENSE` says Apache 2.0). Low legal risk since the `LICENSE` file governs, but creates
confusion for automated tooling and downstream consumers.

---

## Sources Cited

1. mem0 LICENSE: `https://github.com/mem0ai/mem0/blob/main/LICENSE`
2. Chroma LICENSE: `https://github.com/chroma-core/chroma/blob/main/LICENSE`
3. Graphiti (Zep) LICENSE: `https://github.com/getzep/graphiti/blob/main/LICENSE`
4. Letta LICENSE: `https://github.com/letta-ai/letta/blob/main/LICENSE`
5. SurrealDB license page: `https://surrealdb.com/license`
6. SurrealDB Python SDK LICENSE: `https://github.com/surrealdb/surrealdb.py/blob/main/LICENSE`
7. DuckDB LICENSE: `https://github.com/duckdb/duckdb/blob/main/LICENSE`
8. LongMemEval dataset (HuggingFace): `https://huggingface.co/datasets/xiaowu0162/longmemeval`
9. LongMemEval cleaned dataset: `https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned`
10. LongMemEval GitHub LICENSE: `https://github.com/xiaowu0162/LongMemEval/blob/main/LICENSE`
11. LoCoMo LICENSE: `https://github.com/snap-research/locomo/blob/main/LICENSE.txt`
12. pgvector LICENSE: `https://github.com/pgvector/pgvector/blob/master/LICENSE`
13. highlight.js LICENSE: `https://github.com/highlightjs/highlight.js/blob/main/LICENSE`
14. marked LICENSE: `https://marked.js.org/license`
15. DOMPurify LICENSE (Apache 2.0): `https://github.com/cure53/DOMPurify/blob/main/LICENSE`
15b. DOMPurify LICENSE-MPL (MPL 2.0): `https://github.com/cure53/DOMPurify/blob/main/LICENSE-MPL`
16. cross-encoder/nli-deberta-v3-small: `https://huggingface.co/cross-encoder/nli-deberta-v3-small`
17. sentence-transformers LICENSE: `https://github.com/UKPLab/sentence-transformers/blob/master/LICENSE`
18. OpenTelemetry Python LICENSE: `https://github.com/open-telemetry/opentelemetry-python/blob/main/LICENSE`
19. networkx license: `https://github.com/networkx/networkx/blob/main/LICENSE.txt`
20. Pinecone legal: `https://www.pinecone.io/legal/master-subscription-agreement/`

Total sources consulted: 20 URLs.
